"""模拟交易 (实时撮合) —— 资金 / 持仓 / 委托 / 成交 / 回合 + T+1 + 5% 成交额规则

数据源: ReplayFeed.get_kline(code) 的最后一根日K (最新真实行情, 不受回放时钟截断)
时钟: 以 feed.get_trading_days()[-1] 作为「实时交易日」, 懒滚动释放 T+1 冻结持仓
存储: data/live.db (独立于回放账户, 避免与回放时钟耦合)

撮合: 采用「即时成交」为主 + 限价挂单懒撮合
  市价单 / 限价优于现价 → 立即成交
  限价劣于现价 → PENDING 冻结资金或持仓, 下次查询时 try_fill_pending 撮合
"""
from __future__ import annotations
import json
import sqlite3
import threading
import uuid
from datetime import datetime, date, time
from pathlib import Path
from typing import Optional, List

from ..core.config import settings
from ..models import OrderSide
from .fee import FeeCalculator


DB_PATH = Path(__file__).resolve().parent.parent / "data" / "live.db"

# 资金档位展示名 (key 与 settings.TIER_CASH 对应)
TIER_LABELS = {"basic": "基础", "pro": "进阶", "vip": "专业", "large": "大户"}

# 仓位档: 满仓 / 1/2 / 1/3 / 1/5 / 1/10
POSITION_RATIOS = (1.0, 0.5, 1 / 3, 0.2, 0.1)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class LiveTradeManager:
    """模拟交易管理器 (线程安全单例)"""

    _lock_cls = threading.Lock()
    _instance: Optional["LiveTradeManager"] = None

    def __new__(cls, *args, **kwargs):
        with cls._lock_cls:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
            return cls._instance

    def __init__(self, feed=None, registry=None, db_path: Optional[Path] = None):
        if getattr(self, "_initialized", False):
            if feed is not None:
                self.feed = feed
            if registry is not None:
                self.registry = registry
            return
        self.feed = feed
        self.registry = registry
        self.db_path = Path(db_path) if db_path else DB_PATH
        self._lock = threading.RLock()
        self._init_db()
        self._initialized = True

    # ================= DB =================

    def _conn(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS live_accounts (
                user_id TEXT PRIMARY KEY, tier TEXT NOT NULL DEFAULT 'basic',
                cash REAL NOT NULL DEFAULT 0, frozen_cash REAL NOT NULL DEFAULT 0,
                updated_at TEXT)""")
            c.execute("""CREATE TABLE IF NOT EXISTS live_positions (
                user_id TEXT NOT NULL, code TEXT NOT NULL, name TEXT DEFAULT '',
                quantity INTEGER NOT NULL DEFAULT 0, available INTEGER NOT NULL DEFAULT 0,
                pending_in INTEGER NOT NULL DEFAULT 0, avg_cost REAL NOT NULL DEFAULT 0,
                open_date TEXT, open_price REAL NOT NULL DEFAULT 0,
                cost_basis REAL NOT NULL DEFAULT 0, realized_pnl REAL NOT NULL DEFAULT 0,
                trades_count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT, PRIMARY KEY (user_id, code))""")
            c.execute("""CREATE TABLE IF NOT EXISTS live_orders (
                order_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, code TEXT NOT NULL,
                name TEXT DEFAULT '', side TEXT NOT NULL, type TEXT NOT NULL,
                price REAL NOT NULL DEFAULT 0, quantity INTEGER NOT NULL,
                filled_qty INTEGER NOT NULL DEFAULT 0, filled_price REAL NOT NULL DEFAULT 0,
                amount REAL NOT NULL DEFAULT 0, fee REAL NOT NULL DEFAULT 0,
                frozen_amount REAL NOT NULL DEFAULT 0, notional REAL NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'PENDING', reject_reason TEXT DEFAULT '',
                trade_date TEXT, created_at TEXT, updated_at TEXT)""")
            c.execute("""CREATE TABLE IF NOT EXISTS live_trades (
                trade_id TEXT PRIMARY KEY, order_id TEXT, user_id TEXT NOT NULL,
                code TEXT NOT NULL, name TEXT DEFAULT '', side TEXT NOT NULL,
                price REAL NOT NULL, quantity INTEGER NOT NULL, amount REAL NOT NULL,
                fee REAL NOT NULL DEFAULT 0, trade_date TEXT, created_at TEXT)""")
            c.execute("""CREATE TABLE IF NOT EXISTS live_round_trips (
                trip_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, code TEXT NOT NULL,
                name TEXT DEFAULT '', direction TEXT DEFAULT 'long', quantity INTEGER,
                open_date TEXT, open_price REAL, close_date TEXT, close_price REAL,
                amount REAL DEFAULT 0, fee REAL DEFAULT 0, pnl REAL DEFAULT 0,
                pnl_pct REAL DEFAULT 0, hold_days INTEGER DEFAULT 0,
                trades_count INTEGER DEFAULT 0, max_drawdown REAL DEFAULT 0,
                bars_json TEXT DEFAULT '{}', strategy_id INTEGER, note TEXT DEFAULT '',
                created_at TEXT)""")
            c.execute("""CREATE TABLE IF NOT EXISTS live_prefs (
                user_id TEXT PRIMARY KEY, tier TEXT NOT NULL DEFAULT 'basic')""")
            c.execute("""CREATE TABLE IF NOT EXISTS live_state (
                user_id TEXT PRIMARY KEY, last_date TEXT)""")
            c.execute("""CREATE TABLE IF NOT EXISTS live_conditionals (
                cond_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, code TEXT NOT NULL,
                name TEXT DEFAULT '', kind TEXT NOT NULL, trigger_price REAL NOT NULL,
                ratio REAL NOT NULL DEFAULT 1, side TEXT NOT NULL DEFAULT 'long',
                status TEXT NOT NULL DEFAULT 'active', created_price REAL DEFAULT 0,
                created_at TEXT, triggered_at TEXT, triggered_date TEXT,
                triggered_price REAL, triggered_qty INTEGER, order_id TEXT,
                message TEXT DEFAULT '')""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_live_trades ON live_trades(user_id, trade_date)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_live_orders ON live_orders(user_id, status)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_live_trips ON live_round_trips(user_id, close_date)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_live_conds ON live_conditionals(user_id, status)")

    # ================= 时钟 =================

    def _today(self) -> str:
        """实时交易日 = 交易日历中 <= 今日 的最后一天 (可被测试覆写)

        交易日历由 akshare 提供且延伸到年末, 若直接取 days[-1] 会得到未来日期,
        导致 T+1 永远无法结算; 故仅取不晚于真实当天的最近交易日。
        """
        try:
            days = self.feed.get_trading_days() if self.feed else None
            if days:
                real_today = date.today()
                past = [d for d in days if d <= real_today]
                return str(past[-1] if past else days[-1])
        except Exception:
            pass
        return date.today().isoformat()

    def _is_market_open(self) -> bool:
        """是否处于可下单时段: 今日为交易日 且 当前在交易时段内

        非交易时间不存在新成交价, 撮合成交无意义; 关闭 settings.ENFORCE_TRADING_TIME
        后可直接下单 (供联调/演示)。
        """
        if not getattr(settings, "ENFORCE_TRADING_TIME", True):
            return True
        is_open_day = True
        fn = getattr(self.feed, "market_ref_day", None)
        if callable(fn):
            try:
                _, is_open_day = fn()
            except Exception:
                is_open_day = True
        if not is_open_day:
            return False
        now = datetime.now().time()
        morning = (time.fromisoformat(settings.MORNING_OPEN)
                   <= now <= time.fromisoformat(settings.MORNING_CLOSE))
        afternoon = (time.fromisoformat(settings.AFTERNOON_OPEN)
                     <= now <= time.fromisoformat(settings.AFTERNOON_CLOSE))
        return morning or afternoon

    # ================= 档位 =================

    def list_tiers(self) -> List[dict]:
        return [
            {"key": k, "name": TIER_LABELS.get(k, k), "initial_cash": v}
            for k, v in settings.TIER_CASH.items()
        ]

    def get_tier(self, user_id: str) -> str:
        with self._conn() as c:
            row = c.execute("SELECT tier FROM live_prefs WHERE user_id=?", (user_id,)).fetchone()
        return row["tier"] if row else "basic"

    def _scope(self, user_id: str) -> str:
        """作用域键 = 真实用户 + 当前资金体系

        除 live_prefs 外, 所有 live_* 表的 user_id 列存该作用域键, 使各资金体系账户
        完全独立 (资金/持仓/委托/成交/回合互不影响); live_prefs 仍以真实 user_id 为主键,
        仅记录「当前体系」。
        """
        return f"{user_id}#{self.get_tier(user_id)}"

    def bind_tier(self, user_id: str, tier: str) -> dict:
        """切换资金体系 (仅切换当前体系视图, 不重置任何体系账户)"""
        if tier not in settings.TIER_CASH:
            raise ValueError(f"未知资金档位: {tier}")
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO live_prefs (user_id, tier) VALUES (?,?) "
                      "ON CONFLICT(user_id) DO UPDATE SET tier=excluded.tier", (user_id, tier))
        return self.get_account(user_id)

    def reset(self, user_id: str) -> dict:
        """重置「当前体系」资金/持仓/委托/成交 (保留其他体系 + 历史回合)"""
        tier = self.get_tier(user_id)
        with self._lock, self._conn() as c:
            self._reset_locked(c, f"{user_id}#{tier}", tier)
        return self.get_account(user_id)

    def _reset_locked(self, c: sqlite3.Connection, scope: str, tier: str):
        init = settings.TIER_CASH.get(tier, settings.INITIAL_CASH)
        c.execute("INSERT INTO live_accounts (user_id, tier, cash, frozen_cash, updated_at) "
                  "VALUES (?,?,?,0,?) ON CONFLICT(user_id) DO UPDATE SET "
                  "tier=excluded.tier, cash=excluded.cash, frozen_cash=0, updated_at=excluded.updated_at",
                  (scope, tier, init, _now()))
        c.execute("DELETE FROM live_positions WHERE user_id=?", (scope,))
        c.execute("DELETE FROM live_orders WHERE user_id=?", (scope,))
        c.execute("DELETE FROM live_trades WHERE user_id=?", (scope,))
        c.execute("INSERT INTO live_state (user_id, last_date) VALUES (?,?) "
                  "ON CONFLICT(user_id) DO UPDATE SET last_date=excluded.last_date",
                  (scope, self._today()))

    def _ensure_account(self, scope: str, tier: str) -> dict:
        with self._conn() as c:
            row = c.execute("SELECT * FROM live_accounts WHERE user_id=?", (scope,)).fetchone()
        if row:
            return dict(row)
        init = settings.TIER_CASH.get(tier, settings.INITIAL_CASH)
        with self._conn() as c:
            c.execute("INSERT OR IGNORE INTO live_accounts (user_id, tier, cash, frozen_cash, updated_at) "
                      "VALUES (?,?,?,0,?)", (scope, tier, init, _now()))
        return {"user_id": scope, "tier": tier, "cash": init, "frozen_cash": 0.0}

    # ================= T+1 懒结算 =================

    def _roll_settlement(self, scope: str) -> None:
        today = self._today()
        with self._lock, self._conn() as c:
            row = c.execute("SELECT last_date FROM live_state WHERE user_id=?", (scope,)).fetchone()
            if not row:
                c.execute("INSERT OR REPLACE INTO live_state (user_id, last_date) VALUES (?,?)",
                          (scope, today))
                return
            if today > row["last_date"]:
                # 上一交易日及之前买入的持仓 → 解锁为可卖 (T+1)
                c.execute("UPDATE live_positions SET available = available + pending_in, pending_in = 0 "
                          "WHERE user_id=? AND pending_in > 0 AND (open_date IS NULL OR open_date < ?)",
                          (scope, today))
                c.execute("UPDATE live_state SET last_date=? WHERE user_id=?", (today, scope))

    # ================= 行情快照 =================

    def market_snapshot(self, code: str) -> Optional[dict]:
        if not self.feed:
            return None
        kl = self.feed.get_kline(code)
        if not kl:
            return None
        last = kl[-1]
        prev = kl[-2] if len(kl) > 1 else last
        info = (self.registry.get(code) if self.registry else None) or {}
        is_kcb = bool(info.get("is_kcb"))
        pct = 0.20 if is_kcb else 0.10
        prev_close = round(float(prev.close), 2)
        price = round(float(last.close), 2)
        volume = int(getattr(last, "volume", 0) or 0)
        # 成交额展示兜底: 末根 → 近 5 根均值
        amount = float(getattr(last, "amount", 0) or 0)
        if amount <= 0:
            tail = [float(getattr(k, "amount", 0) or 0) for k in kl[-5:]]
            tail = [a for a in tail if a > 0]
            amount = sum(tail) / len(tail) if tail else 0.0
        # 单笔上限基准 = 上一交易日成交额; 兜底链: 上一交易日 → 末根 → 近 5 根均值 → 0
        last_amount = float(getattr(last, "amount", 0) or 0)
        prev_amount = float(getattr(prev, "amount", 0) or 0)
        basis = prev_amount if prev_amount > 0 else last_amount
        if basis <= 0:
            tail5 = [float(getattr(k, "amount", 0) or 0) for k in kl[-6:-1]]
            tail5 = [a for a in tail5 if a > 0]
            basis = sum(tail5) / len(tail5) if tail5 else 0.0
        cap = round(basis * settings.MAX_ORDER_AMOUNT_RATIO, 2)
        limit_up = round(prev_close * (1 + pct), 2)
        limit_down = round(prev_close * (1 - pct), 2)
        return {
            "code": code,
            "name": info.get("name", code),
            "price": price,
            "prev_close": prev_close,
            "open": round(float(last.open), 2),
            "high": round(float(last.high), 2),
            "low": round(float(last.low), 2),
            "volume": volume,
            "amount": round(amount, 2),
            "basis_amount": round(basis, 2),
            "basis_date": prev.timestamp.strftime("%Y-%m-%d"),
            "change_pct": round((price - prev_close) / prev_close * 100, 2) if prev_close else 0.0,
            "trade_date": last.timestamp.strftime("%Y-%m-%d"),
            "suspended": volume == 0,
            "is_kcb": is_kcb,
            "limit_up": limit_up,
            "limit_down": limit_down,
            "at_limit_up": price >= limit_up - 1e-6,
            "at_limit_down": price <= limit_down + 1e-6,
            "max_order_amount": cap,
            "max_order_ratio": settings.MAX_ORDER_AMOUNT_RATIO,
        }

    def max_order_amount(self, code: str) -> float:
        snap = self.market_snapshot(code)
        return snap["max_order_amount"] if snap else 0.0

    # ================= 下单 =================

    def place_order(self, user_id: str, code: str, side: str, otype: str,
                    price: Optional[float] = None, quantity: Optional[int] = None,
                    ratio: Optional[float] = None) -> dict:
        side = (side or "").upper()
        otype = (otype or "LIMIT").upper()
        if side not in ("BUY", "SELL"):
            raise ValueError("买卖方向须为 BUY / SELL")
        if otype not in ("LIMIT", "MARKET"):
            raise ValueError("委托类型须为 LIMIT / MARKET")

        tier = self.get_tier(user_id)
        scope = f"{user_id}#{tier}"
        self._roll_settlement(scope)
        snap = self.market_snapshot(code)
        if not snap:
            raise ValueError("该股暂无行情数据, 无法交易")
        if not self._is_market_open():
            raise ValueError("当前非交易时间, 无法下单")
        if snap["suspended"]:
            raise ValueError("该股当前停牌, 无法交易")
        if side == "BUY" and snap["at_limit_up"]:
            raise ValueError("该股已封涨停, 无法买入")

        today = self._today()
        acct = self._ensure_account(scope, tier)

        # 委托价
        if otype == "MARKET":
            px = snap["price"]
        else:
            px = round(float(price or 0), 2)
            if px <= 0:
                raise ValueError("限价委托必须填写委托价")
            if px > snap["limit_up"] or px < snap["limit_down"]:
                raise ValueError(f"委托价须在涨跌停区间 [{snap['limit_down']}, {snap['limit_up']}]")

        # 手数 (100 股整数倍)
        qty = int(quantity) if quantity else 0
        if qty <= 0 and ratio is not None:
            qty = self._qty_by_ratio(scope, code, side, px, float(ratio), acct)
        if qty <= 0:
            raise ValueError("委托数量必须大于 0")
        if qty % 100 != 0:
            raise ValueError("A 股委托数量须为 100 股的整数倍")

        # ---- 5% 成交额规则 (含当日同向累计, 防拆单) ----
        cap = snap["max_order_amount"]
        if cap <= 0:
            raise ValueError("该股当日无成交额数据, 暂不可委托")
        notional = round(px * qty, 2)
        if notional > cap:
            raise ValueError(f"单笔委托金额 ¥{notional:,.0f} 超过该股当日成交额的 "
                             f"{settings.MAX_ORDER_AMOUNT_RATIO:.0%} (上限 ¥{cap:,.0f})")
        used = self._committed_today(scope, code, side, today)
        if used + notional > cap:
            raise ValueError(f"当日同向累计委托 ¥{used + notional:,.0f} 超过该股当日成交额的 "
                             f"{settings.MAX_ORDER_AMOUNT_RATIO:.0%} (上限 ¥{cap:,.0f})")

        fee = FeeCalculator.calculate(notional, OrderSide(side))
        order_id = str(uuid.uuid4())

        # ---- 资金 / 持仓冻结 ----
        with self._lock, self._conn() as c:
            if side == "BUY":
                need = round(notional + fee, 2)
                row = c.execute("SELECT cash, frozen_cash FROM live_accounts WHERE user_id=?",
                                (scope,)).fetchone()
                cash = row["cash"] if row else 0.0
                if cash < need:
                    raise ValueError(f"可用资金不足: 需 ¥{need:,.2f}, 可用 ¥{cash:,.2f}")
                c.execute("UPDATE live_accounts SET cash=?, frozen_cash=?, updated_at=? WHERE user_id=?",
                          (round(cash - need, 2), round((row["frozen_cash"] or 0) + need, 2),
                           _now(), scope))
                frozen = need
            else:
                pos = c.execute("SELECT available FROM live_positions WHERE user_id=? AND code=?",
                                (scope, code)).fetchone()
                avail = pos["available"] if pos else 0
                if avail < qty:
                    raise ValueError(f"可卖数量不足: 可用 {avail} 股 (T+1 当日买入次日方可卖出)")
                c.execute("UPDATE live_positions SET available=available-?, updated_at=? "
                          "WHERE user_id=? AND code=?", (qty, _now(), scope, code))
                frozen = 0.0

            c.execute("""INSERT INTO live_orders
                (order_id, user_id, code, name, side, type, price, quantity, filled_qty,
                 filled_price, amount, fee, frozen_amount, notional, status, trade_date,
                 created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,0,0,0,?,?,?,'PENDING',?,?,?)""",
                (order_id, scope, code, snap["name"], side, otype, px, qty,
                 fee, frozen, notional, today, _now(), _now()))

        # ---- 即时成交判定 ----
        fillable = (otype == "MARKET"
                    or (side == "BUY" and snap["price"] <= px)
                    or (side == "SELL" and snap["price"] >= px))
        if fillable:
            self._fill_order(scope, order_id, snap["price"], today)

        return self.get_order(scope, order_id)

    def _qty_by_ratio(self, user_id: str, code: str, side: str, px: float,
                      ratio: float, acct: dict) -> int:
        ratio = min(max(ratio, 0.0), 1.0)
        if side == "BUY":
            budget = float(acct.get("cash") or 0) * ratio
            return int(budget // (px * 100)) * 100
        with self._conn() as c:
            row = c.execute("SELECT available FROM live_positions WHERE user_id=? AND code=?",
                            (user_id, code)).fetchone()
        avail = row["available"] if row else 0
        return int((avail * ratio) // 100) * 100

    def _committed_today(self, user_id: str, code: str, side: str, today: str) -> float:
        with self._conn() as c:
            t = c.execute("SELECT COALESCE(SUM(amount),0) AS s FROM live_trades "
                          "WHERE user_id=? AND code=? AND side=? AND trade_date=?",
                          (user_id, code, side, today)).fetchone()["s"]
            o = c.execute("SELECT COALESCE(SUM(notional),0) AS s FROM live_orders "
                          "WHERE user_id=? AND code=? AND side=? AND trade_date=? AND status='PENDING'",
                          (user_id, code, side, today)).fetchone()["s"]
        return round(float(t) + float(o), 2)

    def _fill_order(self, user_id: str, order_id: str, price: float, today: str) -> Optional[dict]:
        """成交: 落成交记录 + 更新持仓 + 结算资金 + 释放冻结"""
        price = round(float(price), 2)
        with self._lock, self._conn() as c:
            o = c.execute("SELECT * FROM live_orders WHERE order_id=?", (order_id,)).fetchone()
            if not o or o["status"] != "PENDING":
                return None
            qty = o["quantity"] - o["filled_qty"]
            if qty <= 0:
                return None
            side = o["side"]
            code = o["code"]
            name = o["name"]
            amount = round(price * qty, 2)
            fee = FeeCalculator.calculate(amount, OrderSide(side))
            acct = c.execute("SELECT * FROM live_accounts WHERE user_id=?", (user_id,)).fetchone()
            cash = float(acct["cash"]) if acct else 0.0
            frozen = float(acct["frozen_cash"]) if acct else 0.0
            pos = c.execute("SELECT * FROM live_positions WHERE user_id=? AND code=?",
                            (user_id, code)).fetchone()
            full_close = False

            if side == "BUY":
                # 冻结按本单本次成交比例释放
                release = round(float(o["frozen_amount"] or 0) * qty / o["quantity"], 2) if o["quantity"] else 0.0
                release = min(frozen, release)
                cost = round(amount + fee, 2)
                cash = round(cash + release - cost, 2)
                frozen = round(max(0.0, frozen - release), 2)
                if pos:
                    old_qty = pos["quantity"]
                    new_qty = old_qty + qty
                    avg_cost = round((pos["avg_cost"] * old_qty + cost) / new_qty, 4) if new_qty else 0
                    c.execute("""UPDATE live_positions SET quantity=?, pending_in=pending_in+?,
                        avg_cost=?, cost_basis=cost_basis+?, trades_count=trades_count+1,
                        open_date=COALESCE(open_date, ?), updated_at=? WHERE user_id=? AND code=?""",
                              (new_qty, qty, avg_cost, cost, today, _now(), user_id, code))
                else:
                    c.execute("""INSERT INTO live_positions
                        (user_id, code, name, quantity, available, pending_in, avg_cost,
                         open_date, open_price, cost_basis, realized_pnl, trades_count, updated_at)
                        VALUES (?,?,?,?,0,?,?,?,?,?,0,1,?)""",
                              (user_id, code, name, qty, qty, round(cost / qty, 4),
                               today, round(cost / qty, 4), cost, _now()))
            else:  # SELL
                cash = round(cash + amount - fee, 2)
                pos_qty = pos["quantity"] if pos else 0
                new_qty = max(0, pos_qty - qty)
                cost_part = round(pos["cost_basis"] * (qty / pos_qty), 2) if pos_qty else 0.0
                realized = round((amount - fee) - cost_part, 2)
                if pos:
                    c.execute("""UPDATE live_positions SET quantity=?, cost_basis=?,
                        realized_pnl=realized_pnl+?, trades_count=trades_count+1, updated_at=?
                        WHERE user_id=? AND code=?""",
                              (new_qty, round(pos["cost_basis"] - cost_part, 2), realized, _now(),
                               user_id, code))
                if new_qty == 0 and pos:
                    full_close = True

            c.execute("UPDATE live_accounts SET cash=?, frozen_cash=?, updated_at=? WHERE user_id=?",
                      (cash, frozen, _now(), user_id))
            c.execute("""INSERT INTO live_trades
                (trade_id, order_id, user_id, code, name, side, price, quantity, amount, fee,
                 trade_date, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (str(uuid.uuid4()), order_id, user_id, code, name, side, price, qty,
                 amount, fee, today, _now()))
            c.execute("""UPDATE live_orders SET filled_qty=filled_qty+?, filled_price=?,
                amount=?, fee=?, status='FILLED', updated_at=? WHERE order_id=?""",
                (qty, price, amount, fee, _now(), order_id))
            if full_close:
                updated = c.execute("SELECT * FROM live_positions WHERE user_id=? AND code=?",
                                    (user_id, code)).fetchone()
                self._close_trip(c, user_id, updated, code, name, price, today)
        return {"order_id": order_id, "price": price, "quantity": qty, "amount": amount, "fee": fee}

    def _close_trip(self, c: sqlite3.Connection, user_id: str, pos: sqlite3.Row, code: str,
                    name: str, close_price: float, today: str):
        """持仓清零 → 生成一条完整交易回合 (供复盘)"""
        open_date = pos["open_date"] or today
        open_price = round(float(pos["open_price"] or pos["avg_cost"] or 0), 2)
        # pos.quantity 已更新为 0, 用本轮成交汇总开仓数量与成本
        rows = c.execute("SELECT side, quantity, amount, fee FROM live_trades "
                         "WHERE user_id=? AND code=? AND trade_date>=? ORDER BY created_at",
                         (user_id, code, open_date)).fetchall()
        buy_qty = sum(r["quantity"] for r in rows if r["side"] == "BUY")
        total_fee = round(sum(float(r["fee"] or 0) for r in rows), 2)
        total_amount = round(sum(float(r["amount"] or 0) for r in rows), 2)
        realized = round(float(pos["realized_pnl"] or 0), 2)
        # 收益率基准 = 累计买入成本
        invested = round(sum(float(r["amount"] or 0) for r in rows if r["side"] == "BUY"), 2)
        pnl_pct = round(realized / invested * 100, 2) if invested else 0.0
        hold_days = self._hold_days(open_date, today)
        bars_json = self._build_trip_bars(code, open_date, today)
        max_dd = self._max_drawdown(bars_json.get("bars") or [])
        c.execute("""INSERT OR REPLACE INTO live_round_trips
            (trip_id, user_id, code, name, direction, quantity, open_date, open_price,
             close_date, close_price, amount, fee, pnl, pnl_pct, hold_days, trades_count,
             max_drawdown, bars_json, strategy_id, note, created_at)
            VALUES (?,?,?,?,'long',?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,'',?)""",
            (str(uuid.uuid4()), user_id, code, name, buy_qty, open_date, open_price,
             today, round(close_price, 2), total_amount, total_fee, realized, pnl_pct,
             hold_days, len(rows), max_dd, json.dumps(bars_json, ensure_ascii=False), _now()))

    def _hold_days(self, open_date: str, close_date: str) -> int:
        try:
            d1 = datetime.strptime(open_date[:10], "%Y-%m-%d").date()
            d2 = datetime.strptime(close_date[:10], "%Y-%m-%d").date()
            return max(0, (d2 - d1).days)
        except (ValueError, TypeError):
            return 0

    def _build_trip_bars(self, code: str, open_date: str, close_date: str) -> dict:
        """构造复盘用 K 线: pre=开仓前背景(≈60根), bars=开仓~平仓(带买卖点)"""
        empty = {"pre": [], "bars": []}
        if not self.feed:
            return empty
        kl = self.feed.get_kline(code)
        if not kl:
            return empty
        dates = [k.timestamp.strftime("%Y-%m-%d") for k in kl]

        def norm(k, action=""):
            return {"date": k.timestamp.strftime("%Y-%m-%d"), "open": round(float(k.open), 2),
                    "high": round(float(k.high), 2), "low": round(float(k.low), 2),
                    "close": round(float(k.close), 2), "volume": int(getattr(k, "volume", 0) or 0),
                    "amount": round(float(getattr(k, "amount", 0) or 0), 2), "action": action}

        try:
            i_open = max(i for i, d in enumerate(dates) if d <= open_date)
        except ValueError:
            return empty
        try:
            i_close = max(i for i, d in enumerate(dates) if d <= close_date)
        except ValueError:
            i_close = len(kl) - 1
        i_close = max(i_close, i_open)

        pre_start = max(0, i_open - 60)
        pre = [norm(k) for k in kl[pre_start:i_open]]
        bars = [norm(k) for k in kl[i_open:i_close + 1]]
        if bars:
            bars[0]["action"] = "BUY"
            bars[-1]["action"] = "SELL" if len(bars) > 1 else "BUY"
        return {"pre": pre, "bars": bars}

    @staticmethod
    def _max_drawdown(bars: List[dict]) -> float:
        peak, dd = None, 0.0
        for b in bars:
            c = float(b.get("close") or 0)
            peak = c if peak is None else max(peak, c)
            if peak:
                dd = max(dd, (peak - c) / peak * 100)
        return round(dd, 2)

    def cancel_order(self, user_id: str, order_id: str) -> bool:
        """撤单: 解冻资金 / 归还锁定持仓 (仅作用于当前体系)"""
        scope = self._scope(user_id)
        with self._lock, self._conn() as c:
            o = c.execute("SELECT * FROM live_orders WHERE order_id=? AND user_id=?",
                          (order_id, scope)).fetchone()
            if not o or o["status"] != "PENDING":
                return False
            remain = o["quantity"] - o["filled_qty"]
            if o["side"] == "BUY":
                release = round(float(o["frozen_amount"] or 0), 2)
                acct = c.execute("SELECT cash, frozen_cash FROM live_accounts WHERE user_id=?",
                                 (scope,)).fetchone()
                c.execute("UPDATE live_accounts SET cash=?, frozen_cash=?, updated_at=? WHERE user_id=?",
                          (round(float(acct["cash"]) + release, 2),
                           round(max(0.0, float(acct["frozen_cash"]) - release), 2), _now(), scope))
            else:
                c.execute("UPDATE live_positions SET available=available+?, updated_at=? "
                          "WHERE user_id=? AND code=?", (remain, _now(), scope, o["code"]))
            c.execute("UPDATE live_orders SET status='CANCELED', updated_at=? WHERE order_id=?",
                      (_now(), order_id))
        return True

    def try_fill_pending(self, user_id: str) -> List[dict]:
        """懒撮合: 现价触及限价时成交挂单 (仅当前体系); 闭市不撮合"""
        self._check_conditionals(user_id)
        if not self._is_market_open():
            return []
        scope = self._scope(user_id)
        self._roll_settlement(scope)
        today = self._today()
        with self._conn() as c:
            rows = c.execute("SELECT order_id, code, side, type, price FROM live_orders "
                             "WHERE user_id=? AND status='PENDING'", (scope,)).fetchall()
        done = []
        for o in rows:
            snap = self.market_snapshot(o["code"])
            if not snap or snap["suspended"]:
                continue
            px = snap["price"]
            # 封板价为最高价 → 买单买不到
            if o["side"] == "BUY" and px >= snap["limit_up"] - 1e-6:
                continue
            if (o["type"] == "MARKET"
                    or (o["side"] == "BUY" and px <= o["price"])
                    or (o["side"] == "SELL" and px >= o["price"])):
                r = self._fill_order(scope, o["order_id"], px, today)
                if r:
                    done.append(r)
        return done

    # ================= 条件单 (止盈 / 止损, 触发即尝试操作) =================

    @staticmethod
    def _serialize_cond(row) -> dict:
        d = dict(row)
        d["kind_label"] = "止盈" if d.get("kind") == "take_profit" else "止损"
        return d

    def list_conditionals(self, user_id: str, limit: int = 50) -> List[dict]:
        """列出条件单 (先触发历史, 再按创建时间倒序)"""
        self._check_conditionals(user_id)
        scope = self._scope(user_id)
        with self._conn() as c:
            rows = c.execute("SELECT * FROM live_conditionals WHERE user_id=? "
                             "ORDER BY created_at DESC LIMIT ?", (scope, limit)).fetchall()
        return [self._serialize_cond(r) for r in rows]

    def set_conditional(self, user_id: str, code: str, kind: str,
                        trigger_price: float, ratio: float = 1.0) -> dict:
        """挂出止盈/止损条件单 (不要求先有持仓)

        触发时尝试按比例卖出持仓; 无可卖持仓时不做操作并给出提示 (见 _check_conditionals)。
        """
        if kind not in ("take_profit", "stop_loss"):
            raise ValueError("kind 必须为 take_profit / stop_loss")
        tp = round(float(trigger_price or 0), 2)
        if tp <= 0:
            raise ValueError("触发价必须大于 0")
        scope = self._scope(user_id)
        self._roll_settlement(scope)
        snap = self.market_snapshot(code)
        if not snap:
            raise ValueError("该股暂无行情数据, 无法挂条件单")
        price = float(snap["price"] or 0)
        # A 股做多: 止盈价须高于现价, 止损价须低于现价
        if kind == "take_profit" and tp <= price:
            raise ValueError(f"止盈触发价应高于现价 {price}")
        if kind == "stop_loss" and tp >= price:
            raise ValueError(f"止损触发价应低于现价 {price}")
        cond_id = str(uuid.uuid4())
        ratio = max(0.01, min(float(ratio or 1.0), 1.0))
        with self._lock, self._conn() as c:
            c.execute("""INSERT INTO live_conditionals
                (cond_id, user_id, code, name, kind, trigger_price, ratio, side, status,
                 created_price, created_at, message)
                VALUES (?,?,?,?,?,?,?,'long','active',?,?,'')""",
                (cond_id, scope, code, snap["name"], kind, tp, ratio, price, _now()))
            row = c.execute("SELECT * FROM live_conditionals WHERE cond_id=?", (cond_id,)).fetchone()
        return self._serialize_cond(row)

    def cancel_conditional(self, user_id: str, cond_id: str) -> bool:
        scope = self._scope(user_id)
        with self._lock, self._conn() as c:
            row = c.execute("SELECT status FROM live_conditionals WHERE cond_id=? AND user_id=?",
                            (cond_id, scope)).fetchone()
            if not row or row["status"] != "active":
                return False
            c.execute("UPDATE live_conditionals SET status='cancelled' WHERE cond_id=?", (cond_id,))
        return True

    def _check_conditionals(self, user_id: str) -> List[dict]:
        """评估活跃条件单: 触发即尝试卖出, 不可操作则标记 skipped 并给出提示; 闭市不触发"""
        if not self._is_market_open():
            return []
        scope = self._scope(user_id)
        self._roll_settlement(scope)
        today = self._today()
        with self._conn() as c:
            rows = c.execute("SELECT * FROM live_conditionals WHERE user_id=? AND status='active'",
                             (scope,)).fetchall()
        fired: List[dict] = []
        for row in rows:
            snap = self.market_snapshot(row["code"])
            if not snap or snap["suspended"]:
                continue
            px = float(snap["price"] or 0)
            tp = float(row["trigger_price"])
            hit = ((row["kind"] == "take_profit" and px >= tp)
                   or (row["kind"] == "stop_loss" and px <= tp))
            if not hit:
                continue
            with self._conn() as c:
                pos = c.execute("SELECT quantity, available FROM live_positions "
                                "WHERE user_id=? AND code=?", (scope, row["code"])).fetchone()
            qty_total = int(pos["quantity"]) if pos else 0
            avail = int(pos["available"]) if pos else 0
            if qty_total <= 0:
                self._mark_skipped(row["cond_id"], "条件单触发, 但当前无持仓, 已跳过", px, today)
                fired.append(self._get_cond(scope, row["cond_id"]))
                continue
            if avail < 100:
                self._mark_skipped(row["cond_id"], "条件单触发, 但持仓不可卖 (T+1), 已跳过", px, today)
                fired.append(self._get_cond(scope, row["cond_id"]))
                continue
            qty = int((avail * float(row["ratio"])) // 100) * 100
            if qty < 100:
                qty = 100 if avail >= 100 else 0
            if qty <= 0:
                self._mark_skipped(row["cond_id"], "条件单触发, 但可卖数量不足 100 股, 已跳过", px, today)
                fired.append(self._get_cond(scope, row["cond_id"]))
                continue
            try:
                order = self.place_order(user_id, row["code"], "SELL", "MARKET", quantity=qty)
            except ValueError as e:
                self._mark_skipped(row["cond_id"], f"条件单触发但下单失败: {e}", px, today)
                fired.append(self._get_cond(scope, row["cond_id"]))
                continue
            with self._lock, self._conn() as c:
                c.execute("""UPDATE live_conditionals SET status='triggered', triggered_at=?,
                    triggered_date=?, triggered_price=?, triggered_qty=?, order_id=?, message=''
                    WHERE cond_id=?""",
                    (_now(), today, round(float(order.get("filled_price") or px), 2),
                     int(order.get("filled_qty") or qty), order.get("order_id"), row["cond_id"]))
            fired.append(self._get_cond(scope, row["cond_id"]))
        return fired

    def _mark_skipped(self, cond_id: str, message: str, price: float, today: str):
        with self._lock, self._conn() as c:
            c.execute("""UPDATE live_conditionals SET status='skipped', triggered_at=?,
                triggered_date=?, triggered_price=?, message=? WHERE cond_id=?""",
                (_now(), today, round(price, 2), message, cond_id))

    def _get_cond(self, scope: str, cond_id: str) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM live_conditionals WHERE cond_id=?", (cond_id,)).fetchone()
        return self._serialize_cond(row) if row else None

    def get_order(self, scope: str, order_id: str) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM live_orders WHERE order_id=? AND user_id=?",
                            (order_id, scope)).fetchone()
        return dict(row) if row else None

    def list_orders(self, user_id: str, limit: int = 100) -> List[dict]:
        self._check_conditionals(user_id)
        scope = self._scope(user_id)
        with self._conn() as c:
            rows = c.execute("SELECT * FROM live_orders WHERE user_id=? "
                             "ORDER BY created_at DESC LIMIT ?", (scope, limit)).fetchall()
        return [dict(r) for r in rows]

    def list_trades(self, user_id: str, limit: int = 100) -> List[dict]:
        self._check_conditionals(user_id)
        scope = self._scope(user_id)
        with self._conn() as c:
            rows = c.execute("SELECT * FROM live_trades WHERE user_id=? "
                             "ORDER BY created_at DESC LIMIT ?", (scope, limit)).fetchall()
        return [dict(r) for r in rows]

    def list_deliveries(self, user_id: str, limit: int = 200) -> dict:
        """交割单: 每一笔成交 (买入/卖出各一条) 按时间倒序, 供模拟交易复盘展示"""
        scope = self._scope(user_id)
        with self._conn() as c:
            rows = c.execute(
                "SELECT trade_id, code, name, side, price, quantity, amount, fee, "
                "trade_date, created_at FROM live_trades WHERE user_id=? "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?", (scope, limit)).fetchall()
        items = []
        for r in rows:
            d = dict(r)
            d["side_label"] = "买入" if d["side"] == "BUY" else "卖出"
            d["price"] = round(float(d["price"] or 0), 3)
            d["amount"] = round(float(d["amount"] or 0), 2)
            d["fee"] = round(float(d["fee"] or 0), 2)
            items.append(d)
        buy = [x for x in items if x["side"] == "BUY"]
        sell = [x for x in items if x["side"] == "SELL"]
        return {
            "tier": self.get_tier(user_id),
            "items": items,
            "summary": {
                "total": len(items),
                "buy_count": len(buy), "sell_count": len(sell),
                "buy_amount": round(sum(x["amount"] for x in buy), 2),
                "sell_amount": round(sum(x["amount"] for x in sell), 2),
                "fee_total": round(sum(x["fee"] for x in items), 2),
            },
        }

    # ================= 账户 =================

    def get_account(self, user_id: str) -> dict:
        tier = self.get_tier(user_id)
        scope = f"{user_id}#{tier}"
        self._roll_settlement(scope)
        self._check_conditionals(user_id)   # 账户查询时顺带评估条件单 (实时触发)
        acct = self._ensure_account(scope, tier)
        init = settings.TIER_CASH.get(tier, settings.INITIAL_CASH)
        with self._conn() as c:
            rows = c.execute("SELECT * FROM live_positions WHERE user_id=? AND quantity>0 "
                             "ORDER BY code", (scope,)).fetchall()
        positions, mv = [], 0.0
        for p in rows:
            snap = self.market_snapshot(p["code"]) or {}
            price = snap.get("price") or p["avg_cost"]
            value = round(price * p["quantity"], 2)
            profit = round((price - p["avg_cost"]) * p["quantity"], 2)
            mv += value
            positions.append({
                "code": p["code"], "name": p["name"] or snap.get("name", p["code"]),
                "quantity": p["quantity"], "available": p["available"],
                "pending_in": p["pending_in"], "frozen": p["quantity"] - p["available"],
                "avg_cost": round(p["avg_cost"], 3), "price": round(price, 2),
                "market_value": value, "profit": profit,
                "profit_pct": round((price - p["avg_cost"]) / p["avg_cost"] * 100, 2) if p["avg_cost"] else 0.0,
                "open_date": p["open_date"],
            })
        cash = round(float(acct.get("cash") or 0), 2)
        frozen_cash = round(float(acct.get("frozen_cash") or 0), 2)
        mv = round(mv, 2)
        total = round(cash + frozen_cash + mv, 2)
        return {
            "tier": tier,
            "tier_name": TIER_LABELS.get(tier, tier),
            "initial_cash": init,
            "cash": cash,
            "frozen_cash": frozen_cash,
            "available_cash": cash,
            "market_value": mv,
            "total_assets": total,
            "total_pnl": round(total - init, 2),
            "total_pnl_pct": round((total - init) / init * 100, 2) if init else 0.0,
            "float_pnl": round(sum(p["profit"] for p in positions), 2),
            "positions": positions,
            "trade_date": self._today(),
        }

    # ================= 复盘 =================

    def list_history(self, user_id: str, limit: int = 50) -> List[dict]:
        scope = self._scope(user_id)
        with self._conn() as c:
            rows = c.execute("SELECT trip_id, user_id, code, name, direction, quantity, "
                             "open_date, open_price, close_date, close_price, amount, fee, pnl, "
                             "pnl_pct, hold_days, trades_count, max_drawdown, strategy_id, note, "
                             "created_at FROM live_round_trips WHERE user_id=? "
                             "ORDER BY close_date DESC, created_at DESC LIMIT ?",
                             (scope, limit)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            out.append({
                "session_id": d["trip_id"], "kind": "live",
                "status": "settled", "status_label": "已平仓",
                "category": "live", "category_label": "模拟交易",
                "code": d["code"], "code_name": d["name"], "code_revealed": 1,
                "start_date": d["open_date"], "end_date": d["close_date"],
                "bars_total": 0, "bars_used": 0,
                "initial_capital": d["amount"], "final_capital": round(d["amount"] + d["pnl"], 2),
                "pnl": round(d["pnl"], 2), "pnl_pct": round(d["pnl_pct"], 2),
                "trades_count": d["trades_count"], "round_trips": 1,
                "win_count": 1 if d["pnl"] > 0 else 0,
                "win_rate": 100.0 if d["pnl"] > 0 else 0.0,
                "leverage": 1.0, "max_drawdown": d["max_drawdown"], "blown_up": 0,
                "strategy_id": d["strategy_id"], "note": d["note"] or "",
                "created_at": d["created_at"], "settled_at": d["created_at"],
                "user_id": user_id, "hold_days": d["hold_days"],
            })
        return out

    def get_history(self, user_id: str, trip_id: str) -> Optional[dict]:
        scope = self._scope(user_id)
        with self._conn() as c:
            row = c.execute("SELECT * FROM live_round_trips WHERE trip_id=? AND user_id=?",
                            (trip_id, scope)).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            payload = json.loads(d.get("bars_json") or "{}")
        except (ValueError, TypeError):
            payload = {}
        bars = payload.get("bars") or []
        pre = payload.get("pre") or []
        return {
            "session_id": d["trip_id"], "trip_id": d["trip_id"], "kind": "live",
            "status": "settled", "status_label": "已平仓",
            "category": "live", "category_label": "模拟交易",
            "code": d["code"], "code_name": d["name"],
            "start_date": d["open_date"], "end_date": d["close_date"],
            "trades_count": d["trades_count"], "round_trips": 1,
            "win_rate": 100.0 if d["pnl"] > 0 else 0.0,
            "pnl": round(d["pnl"], 2), "pnl_pct": round(d["pnl_pct"], 2),
            "initial_capital": d["amount"], "final_capital": round(d["amount"] + d["pnl"], 2),
            "leverage": 1.0, "max_drawdown": d["max_drawdown"], "blown_up": 0,
            "open_price": d["open_price"], "close_price": d["close_price"],
            "strategy_id": d["strategy_id"], "note": d["note"] or "",
            "hold_days": d["hold_days"], "created_at": d["created_at"],
            "bars": bars, "pre_bars": pre, "pre_count": len(pre),
        }

    def set_trip_meta(self, user_id: str, trip_id: str,
                      strategy_id: Optional[int] = None, note: Optional[str] = None) -> bool:
        scope = self._scope(user_id)
        with self._lock, self._conn() as c:
            row = c.execute("SELECT trip_id FROM live_round_trips WHERE trip_id=? AND user_id=?",
                            (trip_id, scope)).fetchone()
            if not row:
                return False
            if strategy_id is not None:
                c.execute("UPDATE live_round_trips SET strategy_id=? WHERE trip_id=?",
                          (strategy_id, trip_id))
            if note is not None:
                c.execute("UPDATE live_round_trips SET note=? WHERE trip_id=?", (note, trip_id))
        return True
