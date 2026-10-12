"""回放引擎 - 全局交易日时钟 + 逐日推进 + 延迟撮合触发

核心设计 (单一权威时钟):
  - _clock_index / _clock_date 是唯一价格源
  - advance_one_day() 推进 → 统一更新所有持仓/自选股的 OHLCV
  - 触发两个延迟撮合事件:
      1) OPEN  (09:30)  昨日挂的买单 → 今日开盘价成交
      2) CLOSE (15:00)  今日尾盘 → 所有卖出委托以收盘价成交

撮合模式: 延迟撮合
  - BUY 委托: T 日委托 → T+1 日 09:30 以开盘价成交 (T+1 买入)
  - SELL 委托: T 日 14:55 自动以 T 日收盘价成交

这是一个纯历史回放系统, 不再需要 GBM / RealTimeFeed / 订单簿实时撮合。
"""
from __future__ import annotations
import threading
from datetime import date, datetime
from typing import List, Optional, Dict, Callable

from ..models import Stock, Order, OrderSide, OrderType, OrderStatus, Trade, KLine
from .replay_feed import ReplayFeed
from ..data.stock_registry import StockRegistry


class ReplayEngine:
    def __init__(self, feed: ReplayFeed, registry: StockRegistry):
        self._feed = feed
        self._registry = registry
        self._account_mgr = None
        self._matching = None
        self._callbacks: List[Callable] = []
        self._lock = threading.RLock()

        # 时钟
        self._clock_index = -1          # -1 表示未启动
        self._clock_date: Optional[date] = None

        # 可见股票池 (用户自选 + 持仓 + 热门)
        self._watch_codes: List[str] = []

    # ========== 依赖注入 ==========

    def set_account_manager(self, account_mgr):
        self._account_mgr = account_mgr

    def set_matching_engine(self, matching):
        self._matching = matching

    # ========== 时钟 ==========

    @property
    def clock_index(self) -> int:
        return self._clock_index

    @property
    def clock_date(self) -> Optional[date]:
        return self._clock_date

    @property
    def is_running(self) -> bool:
        return self._clock_index >= 0

    def get_all_stocks(self) -> List[Stock]:
        """返回当前可见的、有当日 K 线的股票列表"""
        if self._clock_date is None:
            return []
        result = []
        codes = set(self._watch_codes)
        if self._account_mgr:
            for code in self._account_mgr.get_account().positions:
                codes.add(code)
        for code in codes:
            s = self._build_stock_for_today(code)
            if s:
                result.append(s)
        return result

    def get_stock(self, code: str) -> Optional[Stock]:
        return self._build_stock_for_today(code)

    # ========== 时钟推进 ==========

    def start(self, start_date: Optional[date] = None):
        """启动回放时钟，自动推进到 start_date 或默认起点"""
        self._feed.get_trading_days()
        days = self._feed._trading_days

        if start_date:
            target = start_date
        else:
            # 默认: 取茅台 K 线最新日往前 60 天
            klines = self._feed.get_kline("600519")
            latest = klines[-1].timestamp.date() if klines else days[-2]
            # 找 latest 在交易日历中的位置, 往前推 60 天
            latest_idx = None
            for i, d in enumerate(days):
                if d >= latest:
                    latest_idx = i
                    break
            start_offset = max(0, (latest_idx or len(days) - 2) - 60)
            target = days[start_offset]

        # 定位 target 在交易日历中的位置
        self._clock_index = -1
        for i, d in enumerate(days):
            if d >= target:
                self._clock_index = i - 1
                break
        if self._clock_index < 0:
            self._clock_index = -1

        # 立即推进到第一个交易日
        self.advance_one_day()
        print(f"[ReplayEngine] ✅ 已启动, 当前交易日 {self._clock_date} (index={self._clock_index})")

    def advance_one_day(self) -> Optional[date]:
        """推进一个交易日 → 返回新 clock_date, 到尾部返回 None"""
        days = self._feed._trading_days
        with self._lock:
            self._clock_index += 1
            if self._clock_index >= len(days):
                print("[ReplayEngine] 已到历史尾部")
                self._clock_index = len(days) - 1
                return None

            new_date = days[self._clock_index]
            self._clock_date = new_date

        # 1) 更新所有可见股票的 OHLCV
        self._refresh_all_stocks()

        # 2) 触发延迟撮合: OPEN (昨日买单) + CLOSE (今日卖出)
        if self._matching and self._account_mgr:
            self._matching.on_day_opening(self._account_mgr, self._feed, new_date)
            self._matching.on_day_closing(self._account_mgr, self._feed, new_date)

        # 3) 通知回调 (WebSocket 前端推送)
        for cb in self._callbacks:
            try:
                cb(self._clock_date)
            except Exception as e:
                print(f"[ReplayEngine] 回调异常: {e}")

        print(f"[ReplayEngine] 推进到 {self._clock_date} ({self._clock_index + 1}/{len(days)})")
        return self._clock_date

    def advance_days(self, n: int) -> List[date]:
        """批量推进 n 天"""
        result = []
        for _ in range(n):
            d = self.advance_one_day()
            if d is None:
                break
            result.append(d)
        return result

    def seek_to(self, target_date: date) -> Optional[date]:
        """跳转到指定交易日"""
        days = self._feed._trading_days
        for i, d in enumerate(days):
            if d == target_date:
                with self._lock:
                    self._clock_index = i
                    self._clock_date = d
                self._refresh_all_stocks()
                print(f"[ReplayEngine] 跳转到 {d}")
                return d
        print(f"[ReplayEngine] 未找到交易日 {target_date}")
        return None

    # ========== 可见股票 ==========

    def add_watch(self, code: str):
        if code not in self._watch_codes:
            self._watch_codes.append(code)

    def remove_watch(self, code: str):
        if code in self._watch_codes:
            self._watch_codes.remove(code)

    def set_watch_list(self, codes: List[str]):
        self._watch_codes = list(codes)

    # ========== 内部 ==========

    def _build_stock_for_today(self, code: str) -> Optional[Stock]:
        registry_row = self._registry.get(code)
        if not registry_row:
            return None
        kline = self._feed.get_kline_on(code, self._clock_date)
        if kline is None:
            # 今日停牌/未上市 → 跳过
            return None
        stock = Stock(
            code=code,
            name=registry_row["name"],
            price=kline.close,
            prev_close=kline.open if False else self._prev_close(code),  # 下方会设置
            open_price=kline.open,
            high=kline.high,
            low=kline.low,
            volume=kline.volume,
            amount=kline.amount,
            is_kcb=bool(registry_row.get("is_kcb", 0)),
        )
        stock.prev_close = self._prev_close(code) or kline.open
        return stock

    def _prev_close(self, code: str) -> Optional[float]:
        """昨日收盘价"""
        prev = self._feed._prev_kline(code, self._clock_date)
        return prev.close if prev else None

    def _refresh_all_stocks(self):
        """刷新所有可见股票的当日数据（不返回，只供内部 getter 用）"""
        # 当前实现是 lazy build，这里不需要额外动作
        pass

    # ========== 回调 ==========

    def on_day_advanced(self, cb: Callable[[date], None]):
        self._callbacks.append(cb)
