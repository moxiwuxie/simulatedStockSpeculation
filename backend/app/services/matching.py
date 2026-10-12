"""撮合引擎 v3 - 延迟撮合（历史回放模式）

时间点驱动的撮合:
  - OPEN (09:30)  昨日挂的 BUY 委托 → 今日开盘价成交 (T+1 买入)
  - CLOSE (15:00) 当日所有 SELL 委托 → 今日收盘价成交 (尾盘结算)

不再需要订单簿 heapq、连续撮合、做市商机制。
挂单就是存在 _orders 里，等回放时钟触发 OPEN/CLOSE 时统一撮合。
"""
from __future__ import annotations
import threading
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

from ..models import Order, Trade, OrderSide, OrderType, OrderStatus, Stock, KLine
from .replay_feed import ReplayFeed
from .fee import FeeCalculator


class MatchingEngine:
    def __init__(self):
        self._orders: Dict[str, Order] = {}
        self._trades: List[Trade] = []
        self._lock = threading.RLock()

    # ========== 下单 ==========

    def submit_order(self, order: Order) -> Order:
        """仅记录订单，不撮合。返回订单（挂簿即 PENDING）"""
        with self._lock:
            self._orders[order.order_id] = order
            order.status = OrderStatus.PENDING
        return order

    # ========== OPEN 撮合 (T+1 买入) ==========

    def on_day_opening(self, account_mgr, feed: ReplayFeed, today: date) -> int:
        """昨日挂的 BUY 委托 → 今日开盘价成交。返回成交笔数。"""
        trades_all = []
        with self._lock:
            pending_buys = [
                o for o in self._orders.values()
                if o.status == OrderStatus.PENDING and o.side == OrderSide.BUY
            ]

        for order in pending_buys:
            # 检查涨跌停限制 (基于昨收 → 今日涨停价)
            prev_kline = feed._prev_kline(order.stock_code, today)
            today_kline = feed.get_kline_on(order.stock_code, today)
            if not prev_kline or not today_kline:
                # 停牌/无数据 → 今日不撮合，继续挂单
                continue

            # 涨跌停限制
            is_kcb = self._is_kcb(order.stock_code)
            pct = 0.20 if is_kcb else 0.10
            limit_up = round(prev_kline.close * (1 + pct), 2)
            limit_down = round(prev_kline.close * (1 - pct), 2)

            open_price = today_kline.open
            if open_price >= limit_up:
                # 涨停开盘 → 不成交 (买不到)
                print(f"  [OPEN] {order.stock_code} 涨停开盘 {open_price}, 买单 {order.order_id} 挂单继续")
                continue

            # 限价单: 委托价 >= 开盘价 才成交 (买方希望低买, 所以市价/限价都以开盘价成交)
            # 简化: OPEN 撮合时所有买单都以开盘价成交 (除非涨停)
            fill_qty = order.remaining
            if fill_qty <= 0:
                continue

            amount = round(open_price * fill_qty, 2)
            buyer_fee = round(FeeCalculator.calculate(amount, OrderSide.BUY), 2)

            trade = Trade(
                order_id=order.order_id,
                stock_code=order.stock_code,
                side=OrderSide.BUY,
                price=open_price,
                quantity=fill_qty,
                amount=amount,
                fee=buyer_fee,
                buyer_fee=buyer_fee,
                seller_fee=0,
                buyer_id=order.user_id,
                seller_id="replay_open",
            )
            order.filled_quantity += fill_qty
            order.status = OrderStatus.FILLED if order.remaining == 0 else OrderStatus.PARTIAL
            self._trades.append(trade)
            trades_all.append(trade)

        # 统一结算
        if trades_all:
            account_mgr._settle_all_trades(trades_all, self._build_stock(feed, order.stock_code, today))

        return len(trades_all)

    # ========== CLOSE 撮合 (尾盘卖出) ==========

    def on_day_closing(self, account_mgr, feed: ReplayFeed, today: date) -> int:
        """今日所有 SELL 委托 → 收盘价成交。返回成交笔数。"""
        trades_all = []
        with self._lock:
            pending_sells = [
                o for o in self._orders.values()
                if o.status == OrderStatus.PENDING and o.side == OrderSide.SELL
            ]

        for order in pending_sells:
            prev_kline = feed._prev_kline(order.stock_code, today)
            today_kline = feed.get_kline_on(order.stock_code, today)
            if not today_kline:
                continue  # 停牌

            # 涨跌停限制
            is_kcb = self._is_kcb(order.stock_code)
            pct = 0.20 if is_kcb else 0.10
            prev_close = prev_kline.close if prev_kline else today_kline.open
            limit_up = round(prev_close * (1 + pct), 2)
            limit_down = round(prev_close * (1 - pct), 2)

            close_price = today_kline.close
            if close_price <= limit_down:
                # 跌停 → 卖不出
                continue

            # 限价单: 委托价 <= 收盘价 才成交
            if order.order_type == OrderType.LIMIT and order.price > close_price:
                continue  # 限价卖单高于收盘价 → 不成交

            fill_qty = order.remaining
            if fill_qty <= 0:
                continue

            amount = round(close_price * fill_qty, 2)
            seller_fee = round(FeeCalculator.calculate(amount, OrderSide.SELL), 2)

            trade = Trade(
                order_id=order.order_id,
                stock_code=order.stock_code,
                side=OrderSide.SELL,
                price=close_price,
                quantity=fill_qty,
                amount=amount,
                fee=seller_fee,
                buyer_fee=0,
                seller_fee=seller_fee,
                buyer_id="replay_close",
                seller_id=order.user_id,
            )
            order.filled_quantity += fill_qty
            order.status = OrderStatus.FILLED if order.remaining == 0 else OrderStatus.PARTIAL
            self._trades.append(trade)
            trades_all.append(trade)

        if trades_all:
            # 用最后一笔交易的股票构建 Stock 对象 (简化)
            code = trades_all[-1].stock_code
            account_mgr._settle_all_trades(trades_all, self._build_stock(feed, code, today))

        # CLOSE 后执行日终结算 (T+1 转可用)
        account_mgr.daily_settlement()

        return len(trades_all)

    # ========== 查询 ==========

    def cancel_order(self, order_id: str) -> bool:
        with self._lock:
            order = self._orders.get(order_id)
            if not order or order.status in (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED):
                return False
            order.status = OrderStatus.CANCELED
            return True

    def get_order(self, order_id: str) -> Optional[Order]:
        return self._orders.get(order_id)

    def get_all_orders(self, user_id: Optional[str] = None) -> List[Order]:
        with self._lock:
            if user_id:
                return [o for o in self._orders.values() if o.user_id == user_id]
            return list(self._orders.values())

    def get_trades(self, limit: int = 500) -> List[Trade]:
        with self._lock:
            return self._trades[-limit:]

    def clear(self):
        with self._lock:
            self._orders.clear()
            self._trades.clear()

    # ========== 内部 ==========

    @staticmethod
    def _is_kcb(code: str) -> bool:
        return code.startswith("688") or code.startswith("300")

    @staticmethod
    def _build_stock(feed: ReplayFeed, code: str, day: date) -> Optional[Stock]:
        k = feed.get_kline_on(code, day)
        if not k:
            return None
        prev = feed._prev_kline(code, day)
        return Stock(
            code=code, name="",
            price=k.close,
            prev_close=prev.close if prev else k.open,
            open_price=k.open, high=k.high, low=k.low,
            volume=k.volume, amount=k.amount,
        )
