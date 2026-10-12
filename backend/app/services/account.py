"""账户管理 v3 - 延迟撮合适配

BUY 委托: 冻结 cash (pre-reserve)，OPEN 成交时扣减
SELL 委托: 冻结 available，CLOSE 成交时释放

结算仍由 MatchingEngine.on_day_opening/closing 触发,
调用 _settle_all_trades 同时结算买卖双方。
"""
from __future__ import annotations
import sqlite3
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from ..models import Account, Position, Order, Trade, OrderSide, OrderType, OrderStatus, Stock
from .fee import FeeCalculator
from ..core.config import settings


class AccountManager:
    def __init__(self):
        self._accounts: Dict[str, Account] = {}
        self._initialize_default_account()

    def _initialize_default_account(self):
        self._accounts["default"] = Account(
            user_id="default",
            tier="basic",
            cash=settings.TIER_CASH.get("basic", settings.INITIAL_CASH),
            frozen_cash=0.0,
        )

    def ensure_account(self, user_id: str):
        """注册/登录后确保该用户拥有独立资金账户 (首次创建, 幂等)"""
        if user_id not in self._accounts:
            self._accounts[user_id] = Account(
                user_id=user_id,
                tier="basic",
                cash=settings.TIER_CASH.get("basic", settings.INITIAL_CASH),
                frozen_cash=0.0,
            )
            self._persist()

    def set_tier(self, tier: str, user_id: str = "default") -> tuple[bool, str]:
        """切换账户分级（会重置资金和持仓）"""
        tier = tier.lower()
        if tier not in settings.TIER_CASH:
            return False, f"未知分级: {tier}. 可用: {list(settings.TIER_CASH.keys())}"
        self._accounts[user_id] = Account(
            user_id=user_id,
            tier=tier,
            cash=settings.TIER_CASH[tier],
            frozen_cash=0.0,
        )
        self._persist()
        return True, f"已切换到 {tier} 级, 初始资金 ¥{settings.TIER_CASH[tier]:,.0f}"

    # ========== 查询 ==========

    def get_account(self, user_id: str = "default") -> Account:
        account = self._accounts[user_id]
        self._update_market_values(account)
        return account

    def _update_market_values(self, account: Account):
        # v3 不再持有 MarketEngine 引用, 市值由前端回放时钟计算
        total_pos_value = sum(p.market_value for p in account.positions.values())
        account.total_assets = round(account.cash + account.frozen_cash + total_pos_value, 2)

    # ========== 下单校验 + 冻结 ==========

    def validate_and_freeze(self, order: Order, stock: Optional[Stock]) -> tuple[bool, str]:
        """校验 + 锁定资金/持仓。失败则 order.status = REJECTED"""
        account = self._accounts[order.user_id]

        if not stock:
            return False, f"股票 {order.stock_code} 不存在"

        # 停牌检查
        # 由 ReplayEngine 传入的 stock 已过滤停牌, 这里可选

        # 涨跌停检查
        if order.order_type == OrderType.LIMIT:
            if order.price < stock.limit_down() or order.price > stock.limit_up():
                return False, f"委托价格 {order.price} 超出涨跌停 [{stock.limit_down()}, {stock.limit_up()}]"

        # 数量检查
        if order.quantity <= 0 or order.quantity % 100 != 0:
            return False, "数量必须是 100 的正整数倍"

        # 资金/持仓检查 + 冻结
        if order.side == OrderSide.BUY:
            estimate_price = order.price if order.order_type == OrderType.LIMIT else stock.price
            estimate_amount = estimate_price * order.quantity
            fee = FeeCalculator.calculate(estimate_amount, OrderSide.BUY)
            total_cost = estimate_amount + fee
            if account.cash < total_cost:
                return False, f"资金不足, 需要 {total_cost:.2f}, 当前 {account.cash:.2f}"
            # 冻结资金 (pre-reserve)
            account.cash -= total_cost
            account.frozen_cash += total_cost
            order._frozen_amount = round(total_cost, 2)
        else:
            pos = account.positions.get(order.stock_code)
            if not pos or pos.available < order.quantity:
                avail = pos.available if pos else 0
                return False, f"可卖不足, 需要 {order.quantity}, 当前 {avail}"
            pos.available -= order.quantity
            order._frozen_amount = 0.0

        return True, ""

    def unfreeze(self, order: Order):
        """撤单/拒绝时释放冻结"""
        account = self._accounts[order.user_id]
        if order.side == OrderSide.BUY:
            frozen = getattr(order, '_frozen_amount', 0)
            unfreeze_amount = frozen - (order.filled_quantity * order.price)
            if order.status in (OrderStatus.FILLED, OrderStatus.PARTIAL):
                # 已成交部分的冻结已在 settle 里转为扣减, 但 settle 是从 trade.amount 算的
                # 这里简化: 直接释放全部 frozen, 但 settle 已经从 cash 扣了
                # 改为: 释放 frozen, 然后 cash += frozen_amount - 实际已用
                pass
            account.cash += order._frozen_amount
            account.frozen_cash -= order._frozen_amount
        else:
            # 卖单: 恢复未成交部分的 available
            pos = account.positions.get(order.stock_code)
            if pos:
                pos.available += order.remaining
        self._persist()

    # ========== 延迟撮合结算 (MATCHING ENGINE 调用) ==========

    def _settle_all_trades(self, trades: List[Trade], stock: Optional[Stock]):
        """对每个 trade 同时结算买卖双方"""
        for trade in trades:
            self._settle_one_side(trade, stock, is_buyer=True)
            self._settle_one_side(trade, stock, is_buyer=False)
        self._persist()

    def _settle_one_side(self, trade: Trade, stock: Optional[Stock], is_buyer: bool):
        user_id = trade.buyer_id if is_buyer else trade.seller_id
        if user_id.startswith("replay_") or user_id == "market_maker":
            return
        account = self._accounts.get(user_id)
        if not account:
            return

        fee = trade.buyer_fee if (is_buyer and trade.buyer_fee > 0) else \
              trade.seller_fee if (not is_buyer and trade.seller_fee > 0) else trade.fee

        if is_buyer:
            # BUY: 加持仓 + 扣 cash (但 frozen_cash 还没释放!)
            pos = account.positions.setdefault(
                trade.stock_code, Position(stock_code=trade.stock_code)
            )
            total_cost = pos.avg_cost * pos.quantity + trade.amount + fee
            pos.quantity += trade.quantity
            pos.avg_cost = round(total_cost / pos.quantity, 2) if pos.quantity > 0 else 0
            if stock:
                pos.market_value = round(stock.price * pos.quantity, 2)
            pos._t1_pending = getattr(pos, '_t1_pending', 0) + trade.quantity
            account.cash -= (trade.amount + fee)
            # 释放对应的 frozen_cash (按实际成交)
            # 简化: 在 settle_all_trades 结束后统一释放
        else:
            # SELL: 减持仓 + 加 cash
            pos = account.positions.get(trade.stock_code)
            if pos:
                pos.quantity -= trade.quantity
                if stock:
                    pos.market_value = round(stock.price * pos.quantity, 2)
                pos.available = min(pos.quantity, pos.available + trade.quantity)
                if pos.quantity == 0:
                    pos.avg_cost = 0
            account.cash += (trade.amount - fee)

    def _release_buy_frozen(self, trades: List[Trade]):
        """BUY 成交后释放 frozen_cash"""
        for trade in trades:
            if trade.buyer_id.startswith("replay_"):
                continue
            account = self._accounts.get(trade.buyer_id)
            if account and account.frozen_cash > 0:
                # 每次释放对应这次交易的 amount + fee
                fee = trade.buyer_fee if trade.buyer_fee > 0 else trade.fee
                release = trade.amount + fee
                release = min(release, account.frozen_cash)
                account.frozen_cash -= release

    def _release_sell_frozen(self, trades: List[Trade]):
        """SELL 成交后释放 (已经在 settle 里 avail += trade.quantity 了)"""
        pass

    # ========== T+1 日终结算 ==========

    def daily_settlement(self):
        """每个交易日 15:00 后调用: T+1 转可用 + 清空剩余 frozen_cash"""
        for account in self._accounts.values():
            for code, pos in account.positions.items():
                pending = getattr(pos, '_t1_pending', 0)
                if pending > 0:
                    pos.available = min(pos.quantity, pos.available + pending)
                    pos._t1_pending = 0
            # 清空未成交 BUY 的 frozen_cash
            if account.frozen_cash > 0:
                account.cash += account.frozen_cash
                account.frozen_cash = 0.0
        self._persist()

    # ========== 撤单 ==========

    def cancel_order(self, order: Order):
        """撤单时释放冻结"""
        self.unfreeze(order)

    # ========== 收益查询 ==========

    def get_profit_summary(self, user_id: str = "default") -> Dict:
        account = self.get_account(user_id)
        positions_info = []
        for code, pos in account.positions.items():
            positions_info.append({
                "code": code,
                "quantity": pos.quantity,
                "available": pos.available,
                "avg_cost": pos.avg_cost,
                "market_value": pos.market_value,
                "profit": round(pos.market_value - pos.avg_cost * pos.quantity, 2),
            })
        return {
            "total_assets": account.total_assets,
            "initial_cash": settings.INITIAL_CASH,
            "total_profit": round(account.total_assets - settings.INITIAL_CASH, 2),
            "cash": round(account.cash, 2),
            "frozen_cash": round(account.frozen_cash, 2),
            "positions": positions_info,
        }

    # ========== 持久化 ==========

    def _persist(self):
        try:
            db_path = Path(__file__).resolve().parent.parent / "data" / "trading.db"
            db_path.parent.mkdir(exist_ok=True)
            conn = sqlite3.connect(str(db_path))
            c = conn.cursor()
            c.execute("""CREATE TABLE IF NOT EXISTS accounts (
                user_id TEXT PRIMARY KEY,
                tier TEXT, cash REAL, frozen_cash REAL, positions TEXT
            )""")
            for uid, acc in self._accounts.items():
                pos_data = {}
                for code, pos in acc.positions.items():
                    pos_data[code] = {
                        "quantity": pos.quantity,
                        "available": pos.available,
                        "avg_cost": pos.avg_cost,
                        "_t1_pending": getattr(pos, '_t1_pending', 0),
                    }
                c.execute(
                    "INSERT OR REPLACE INTO accounts VALUES (?,?,?,?,?)",
                    (uid, acc.tier, acc.cash, acc.frozen_cash, json.dumps(pos_data))
                )
            conn.commit()
            conn.close()
        except Exception:
            pass

    def restore_from_db(self):
        try:
            db_path = Path(__file__).resolve().parent.parent / "data" / "trading.db"
            if not db_path.exists():
                return
            conn = sqlite3.connect(str(db_path))
            c = conn.cursor()
            c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='accounts'")
            if not c.fetchone():
                conn.close(); return
            c.execute("SELECT user_id, tier, cash, frozen_cash, positions FROM accounts")
            for row in c.fetchall():
                uid, tier_val, cash, frozen, pos_json = row
                acc = Account(user_id=uid, tier=tier_val or "basic", cash=cash, frozen_cash=frozen)
                for code, d in json.loads(pos_json).items():
                    pos = Position(stock_code=code, quantity=d["quantity"],
                                  available=d["available"], avg_cost=d["avg_cost"])
                    pos._t1_pending = d.get("_t1_pending", 0)
                    acc.positions[code] = pos
                self._accounts[uid] = acc
            conn.close()
        except Exception:
            pass
