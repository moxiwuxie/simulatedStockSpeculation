"""回放数据源 - 按需懒加载 K 线 + 缓存 + 停牌标记

职责:
  - get_kline(code) → List[KLine]  某日的日线（懒加载 akshare → 缓存 → 返回）
  - get_trading_days() → List[date]  交易日历（缓存）
  - detect_suspended(code, date) → bool  停牌判定（成交量为0 且 当日K线存在）

数据源:
  - akshare.stock_zh_a_daily(symbol=f"{sh|sz|bj}{code}", adjust="qfq")
  - akshare.tool_trade_date_hist_sina() → 交易日历
"""
from __future__ import annotations
import threading
import time
from datetime import date, datetime
from typing import List, Optional, Dict
from collections import deque

from ..models import KLine


class ReplayFeed:
    def __init__(self, kline_cache_days: int = 400, kline_ttl: float = 300.0):
        self._kline_cache: Dict[str, List[KLine]] = {}   # code → 全量日K
        self._kline_ts: Dict[str, float] = {}            # code → 缓存写入时间
        self._kline_ttl = kline_ttl                      # 缓存有效期(秒): 保证当日行情能被刷新
        self._trading_days: List[date] = []              # 交易日历
        self._lock = threading.RLock()
        self._cache_days = kline_cache_days

    # ========== 交易日历 ==========

    def get_trading_days(self) -> List[date]:
        with self._lock:
            if self._trading_days:
                return self._trading_days
        self._load_trading_days()
        return self._trading_days

    def _load_trading_days(self):
        import akshare as ak
        df = ak.tool_trade_date_hist_sina()
        days = [datetime.strptime(str(d), "%Y-%m-%d").date() for d in df["trade_date"]]
        with self._lock:
            self._trading_days = days
        print(f"[ReplayFeed] 交易日历 {len(days)} 天, 最新 {days[-1]}")

    def next_trading_day(self, ref_day: date) -> Optional[date]:
        """返回 ref_day 之后的下一个交易日"""
        days = self.get_trading_days()
        for d in days:
            if d > ref_day:
                return d
        return None

    def market_ref_day(self):
        """行情基准日: 今日开市(为交易日)取今日, 不开市取不晚于今日的最近交易日.

        返回 (基准日, 今日是否开市)。交易日历由 akshare 提供且延伸到年末,
        必须用 date.today() 夹取, 否则未来日期会被误判为「开市」。
        """
        days = self.get_trading_days()
        today = date.today()
        if not days:
            return today, False
        if today in days:
            return today, True
        past = [d for d in days if d <= today]
        return (past[-1] if past else days[0]), False

    # ========== K 线 ==========

    def get_kline(self, code: str, start_date: Optional[date] = None) -> List[KLine]:
        """获取某只股票的日K（可指定起始日过滤）

        缓存带 TTL: 过期后重新拉取, 保证行情模块能看到「当天」的日K (接受数据延迟)。
        """
        now = time.time()
        with self._lock:
            klines = self._kline_cache.get(code)
            if klines is not None and now - self._kline_ts.get(code, 0.0) >= self._kline_ttl:
                klines = None                      # 缓存过期 → 重新拉取
        if klines is None:
            klines = self._fetch_kline(code)
            with self._lock:
                self._kline_cache[code] = klines
                self._kline_ts[code] = now
        if start_date:
            return [k for k in klines if k.timestamp.date() >= start_date]
        return klines

    def _fetch_kline(self, code: str) -> List[KLine]:
        """从 akshare 拉取 + 解析"""
        import akshare as ak
        prefix = self._prefix_for(code)
        try:
            df = ak.stock_zh_a_daily(symbol=f"{prefix}{code}", adjust="qfq")
        except Exception as e:
            print(f"[ReplayFeed] 拉取 {code} K 线失败: {e}")
            return []
        if df is None or df.empty:
            return []

        klines = []
        for _, row in df.iterrows():
            d = row["date"]
            if isinstance(d, str):
                d = datetime.strptime(d[:10], "%Y-%m-%d")
            klines.append(KLine(
                open=round(float(row["open"]), 2),
                high=round(float(row["high"]), 2),
                low=round(float(row["low"]), 2),
                close=round(float(row["close"]), 2),
                volume=int(float(row["volume"])),
                amount=float(row["amount"]),
                timestamp=datetime(d.year, d.month, d.day),
            ))
        print(f"[ReplayFeed] {code} 载入 {len(klines)} 条日K")
        return klines

    def get_kline_on(self, code: str, day: date) -> Optional[KLine]:
        """获取某只股票在某一天的日线（None = 停牌或未上市）"""
        klines = self.get_kline(code)
        target = datetime(day.year, day.month, day.day)
        for k in klines:
            if k.timestamp == target:
                return k
        return None

    # ========== 停牌判定 ==========

    def is_suspended(self, code: str, day: date) -> bool:
        """停牌 = 当日 K 线存在但成交量为 0"""
        k = self.get_kline_on(code, day)
        if k is None:
            # 该日无 K 线：可能停牌也可能未上市，用前一日判断
            prev = self._prev_kline(code, day)
            if prev:
                return True  # 有历史但当日缺失 → 停牌
            return False     # 无历史 → 未上市
        return k.volume == 0

    def _prev_kline(self, code: str, day: date) -> Optional[KLine]:
        klines = self.get_kline(code)
        target = datetime(day.year, day.month, day.day)
        prev = None
        for k in klines:
            if k.timestamp >= target:
                break
            prev = k
        return prev

    # ========== 周期聚合 ==========

    @staticmethod
    def aggregate(klines: List[KLine], period: str) -> List[KLine]:
        """日线 → 周线(ISO年周) / 月线(年月) / 年线(年) 聚合; period=day 原样返回"""
        if period == "day":
            return klines
        groups, order = {}, []
        for k in klines:
            d = k.timestamp.date()
            if period == "week":
                key = d.isocalendar()[:2]
            elif period == "month":
                key = (d.year, d.month)
            else:  # year
                key = (d.year,)
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(k)
        result = []
        for key in order:
            g = groups[key]
            result.append(KLine(
                timestamp=datetime(g[0].timestamp.year, g[0].timestamp.month, g[0].timestamp.day),
                open=g[0].open,
                high=max(x.high for x in g),
                low=min(x.low for x in g),
                close=g[-1].close,
                volume=sum(x.volume for x in g),
                amount=sum(x.amount for x in g),
            ))
        return result

    # ========== 工具 ==========

    @staticmethod
    def _prefix_for(code: str) -> str:
        if code.startswith(("6", "9")):
            return "sh"
        if code.startswith(("0", "3")):
            return "sz"
        return "bj"

    def invalidate(self, code: str):
        """清除单只股票的 K 线缓存"""
        with self._lock:
            self._kline_cache.pop(code, None)
            self._kline_ts.pop(code, None)
