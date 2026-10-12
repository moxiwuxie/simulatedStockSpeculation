"""行情引擎 - 真实行情优先 + GBM fallback"""
from __future__ import annotations
import random
import threading
from datetime import datetime, time, timedelta
from typing import Dict, List, Optional
from collections import deque

from ..models import Stock, Tick, KLine
from ..core.config import settings


class MarketEngine:
    """
    行情引擎：
    - 优先使用真实行情（新浪财经/RealTimeFeed）
    - 网络异常时自动降级到 GBM 模拟
    - 历史K线优先真实数据（akshare.stock_zh_a_daily）
    - 管理涨跌停 / 交易时间 / 日终结算
    """

    def __init__(self, realtime_feed=None):
        self._stocks: Dict[str, Stock] = {}
        self._klines: Dict[str, deque] = {}
        self._ticks: Dict[str, deque] = {}
        self._history_cache: Dict[str, List[KLine]] = {}
        self._lock = threading.RLock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._kline_period = 60
        self._account_mgr = None
        self._settlement_done_today = False
        self._feed = realtime_feed
        self._feed_last_seen: float = 0  # feed 最后一次成功的时间戳（秒）

    # ========== 股票管理 ==========

    def add_stock(self, stock: Stock) -> None:
        with self._lock:
            if stock.open_price == 0:
                stock.open_price = stock.price
                stock.high = stock.price
                stock.low = stock.price
            self._stocks[stock.code] = stock
            self._klines[stock.code] = deque(maxlen=1000)
            self._ticks[stock.code] = deque(maxlen=500)

    def get_stock(self, code: str) -> Optional[Stock]:
        with self._lock:
            return self._stocks.get(code)

    def get_all_stocks(self) -> List[Stock]:
        with self._lock:
            return list(self._stocks.values())

    def _get_codes(self) -> List[str]:
        with self._lock:
            return list(self._stocks.keys())

    def set_account_manager(self, account_mgr):
        self._account_mgr = account_mgr

    # ========== 行情更新 ==========

    def apply_realtime_quotes(self, quotes: list) -> None:
        """RealTimeFeed 回调入口 - 批量更新股票"""
        import time as _time
        with self._lock:
            self._feed_last_seen = _time.time()  # 刷新心跳
            for q in quotes:
                stock = self._stocks.get(q.code)
                if not stock:
                    continue
                stock.price = round(q.price, 2)
                stock.high = max(stock.high, q.high) if q.high else stock.high
                stock.low = min(stock.low, q.low) if q.low else stock.low
                stock.open_price = q.open_price if q.open_price else stock.open_price
                # 涨跌幅计算基于昨收
                if q.prev_close and q.prev_close > 0:
                    stock.prev_close = q.prev_close
                if q.volume:
                    stock.volume = q.volume
                if q.amount:
                    stock.amount = q.amount
                # 生成 tick
                self._ticks[q.code].append(
                    Tick(q.code, q.price, 100, datetime.now())
                )

    def update_price_gbm(self, code: str) -> Optional[Tick]:
        """GBM 模拟价格（feed 离线时的 fallback）"""
        with self._lock:
            stock = self._stocks.get(code)
            if not stock:
                return None
            # 涨跌停限制内 GBM
            mu, sigma = 0.0001, 0.002
            shock = random.gauss(mu, sigma)
            new_price = round(stock.price * (1 + shock), 2)
            limit_up = stock.limit_up()
            limit_down = stock.limit_down()
            new_price = max(limit_down, min(limit_up, new_price))
            stock.price = new_price
            stock.high = max(stock.high, new_price)
            stock.low = min(stock.low, new_price)
            tick = Tick(code, new_price, random.randint(100, 1000), datetime.now())
            self._ticks[code].append(tick)
            return tick

    def set_price(self, code: str, price: float) -> None:
        with self._lock:
            stock = self._stocks.get(code)
            if stock and stock.limit_down() <= price <= stock.limit_up():
                stock.price = round(price, 2)
                stock.high = max(stock.high, price)
                stock.low = min(stock.low, price)

    # ========== 交易时间 ==========

    @staticmethod
    def is_trading_time() -> bool:
        now = datetime.now().time()
        morning = time.fromisoformat(settings.MORNING_OPEN) <= now <= time.fromisoformat(settings.MORNING_CLOSE)
        afternoon = time.fromisoformat(settings.AFTERNOON_OPEN) <= now <= time.fromisoformat(settings.AFTERNOON_CLOSE)
        return morning or afternoon

    @staticmethod
    def market_status() -> str:
        return "TRADING" if MarketEngine.is_trading_time() else "CLOSED"

    # ========== 行情循环 ==========

    def start(self) -> None:
        self._running = True
        # 启动 feed（如果配置了）
        if self._feed:
            self._feed.subscribe(self.apply_realtime_quotes)
            self._feed.start(codes_provider=self._get_codes)
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._feed:
            self._feed.stop()

    def _run_loop(self) -> None:
        """行情生成循环 + 日终调度"""
        import time
        FEED_TIMEOUT = 10.0  # 超过10秒没收到 feed 数据视为离线
        while self._running:
            now = datetime.now()

            # 日终结算
            if self._account_mgr and now.hour == 15 and not self._settlement_done_today:
                self._account_mgr.daily_settlement()
                self._settlement_done_today = True
            elif now.hour < 15:
                self._settlement_done_today = False

            # 判断 feed 是否在线
            feed_alive = (time.time() - self._feed_last_seen) < FEED_TIMEOUT and self._feed is not None

            # feed 离线时 fallback GBM（交易时段或模拟模式）
            if not feed_alive:
                with self._lock:
                    for code in list(self._stocks.keys()):
                        self.update_price_gbm(code)

            time.sleep(settings.TICK_INTERVAL)

    # ========== K线 / Tick 查询 ==========

    def get_klines(self, code: str, limit: int = 100) -> List[KLine]:
        with self._lock:
            return list(self._klines.get(code, []))[-limit:]

    def get_ticks(self, code: str, limit: int = 50) -> List[Tick]:
        with self._lock:
            return list(self._ticks.get(code, []))[-limit:]

    # ========== 历史K线 ==========

    def generate_history(self, code: str, days: int = 120) -> List[KLine]:
        """历史K线 - 优先真实数据，fallback GBM，按 days 截断"""
        if code in self._history_cache:
            return self._history_cache[code][-days:]

        history = self._fetch_real_history(code, days)
        if history:
            self._history_cache[code] = history
            return history[-days:]

        history = self._generate_gbm_history(code, days)
        self._history_cache[code] = history
        return history[-days:]

    def _fetch_real_history(self, code: str, days: int) -> List[KLine]:
        """从新浪/akshare 拉真实历史"""
        try:
            import akshare as ak
            # 新浪接口要求 sh/sz 前缀
            prefix = "sh" if code.startswith(("6", "9")) else "sz"
            df = ak.stock_zh_a_daily(symbol=f"{prefix}{code}", adjust="qfq")
            if df is None or df.empty:
                return []
            # 取最近 N 个交易日
            df = df.tail(min(days, len(df)))
            history = []
            for _, row in df.iterrows():
                history.append(KLine(
                    open=round(float(row["open"]), 2),
                    high=round(float(row["high"]), 2),
                    low=round(float(row["low"]), 2),
                    close=round(float(row["close"]), 2),
                    volume=int(float(row["volume"])),
                    amount=float(row["amount"]),
                    timestamp=datetime.fromisoformat(str(row["date"])),
                ))
            # 用最后一天的收盘价更新 prev_close
            if history:
                stock = self._stocks.get(code)
                if stock:
                    last = history[-1]
                    stock.prev_close = last.close
                    stock.price = last.close
                    stock.open_price = last.close
                    stock.high = last.high
                    stock.low = last.low
            return history
        except Exception as e:
            print(f"[MarketEngine] 拉取 {code} 历史K线失败: {e}")
            return []

    def _generate_gbm_history(self, code: str, days: int) -> List[KLine]:
        stock = self._stocks.get(code)
        if not stock:
            return []
        history = []
        base_price = stock.prev_close or stock.price or 10.0
        for i in range(days, 0, -1):
            dt = datetime.now() - timedelta(days=i)
            if dt.weekday() >= 5:
                continue
            volatility = random.uniform(0.01, 0.03)
            open_p = round(base_price * (1 + random.uniform(-0.01, 0.01)), 2)
            close_p = round(open_p * (1 + random.gauss(0, volatility)), 2)
            high_p = round(max(open_p, close_p) * (1 + random.uniform(0, 0.01)), 2)
            low_p = round(min(open_p, close_p) * (1 - random.uniform(0, 0.01)), 2)
            vol = random.randint(500000, 5000000)
            history.append(KLine(open_p, high_p, low_p, close_p, vol, vol * close_p, dt))
            base_price = close_p
        return history
