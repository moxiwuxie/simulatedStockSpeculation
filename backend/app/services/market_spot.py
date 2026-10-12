"""全市场即时快照服务 —— 动态分组指标的数据底座

数据源:
  akshare.stock_zh_a_spot()  (新浪, 一次返回全市场 ≈5500 只, 单次约 19s)

派生字段 (优先集指标全部可由快照直接算出, 无需历史序列):
  - 涨跌停价 / 是否涨停 / 是否跌停 / 是否炸板(曾涨停后开板)
  - 上影线比例% = (最高 - max(今开, 最新)) / 昨收 * 100
  - 涨跌幅 / 成交量 / 成交额 / 最新价 / 今开 / 收盘价

缓存: 默认 300s TTL, 避免每次 19s 的抓取; 拉取失败时回退上一份缓存。
"""
from __future__ import annotations
import threading
import time
from typing import Any, Callable, Dict, Optional

_CODE_PREFIXES = ("bj", "sh", "sz")


def _norm_code(raw: str) -> str:
    c = str(raw or "").strip().lower()
    for p in _CODE_PREFIXES:
        if c.startswith(p):
            return c[len(p):]
    return c


def _split_prefix(raw: str) -> str:
    low = str(raw or "").strip().lower()
    for p in _CODE_PREFIXES:
        if low.startswith(p):
            return p
    return ""


def _limit_pct(prefix: str, code: str, name: str) -> float:
    """涨跌停幅度: 北交所 30% / 创业板·科创板 20% / ST 5% / 其余 10%"""
    if prefix == "bj":
        return 0.30
    if code.startswith(("300", "301", "688", "689")):
        return 0.20
    if "ST" in (name or "").upper():
        return 0.05
    return 0.10


class MarketSpot:
    def __init__(self, ttl: float = 300.0, fetcher: Optional[Callable[[], Any]] = None):
        self._ttl = ttl
        self._fetcher = fetcher          # 测试可注入假数据源
        self._cache: Dict[str, dict] = {}
        self._ts = 0.0
        self._lock = threading.RLock()

    # ---------- 数据源 ----------
    def _fetch(self):
        if self._fetcher is not None:
            return self._fetcher()
        import akshare as ak
        return ak.stock_zh_a_spot()

    def _parse(self, df) -> Dict[str, dict]:
        out: Dict[str, dict] = {}
        if df is None:
            return out
        try:
            records = df.to_dict("records")
        except Exception:
            return out
        for row in records:
            raw = str(row.get("代码") or "").strip()
            if not raw:
                continue
            prefix = _split_prefix(raw)
            code = _norm_code(raw)
            if len(code) != 6 or not code.isdigit():
                continue
            name = str(row.get("名称") or "")

            def num(key: str) -> float:
                try:
                    return float(row.get(key))
                except (TypeError, ValueError):
                    return 0.0

            price = num("最新价")
            prev = num("昨收")
            op = num("今开")
            high = num("最高")
            low = num("最低")
            vol = num("成交量")
            amt = num("成交额")
            pct = num("涨跌幅")

            lp = _limit_pct(prefix, code, name)
            up = round(prev * (1 + lp), 2) if prev else 0.0
            dn = round(prev * (1 - lp), 2) if prev else 0.0
            active = prev > 0 and price > 0
            out[code] = {
                "code": code,
                "name": name,
                "market": prefix or "",
                "price": price,
                "prev_close": prev,
                "open": op,
                "high": high,
                "low": low,
                "volume": int(vol),
                "amount": amt,
                "change_pct": round(pct, 2),
                "limit_pct": lp,
                "limit_up_price": up,
                "limit_down_price": dn,
                "is_limit_up": bool(active and price >= up - 1e-6),
                "is_limit_down": bool(active and price <= dn + 1e-6),
                "is_zhaban": bool(active and high >= up - 1e-6 and price < up - 1e-6),
                "upper_shadow": (round((high - max(op, price)) / prev * 100, 2) if prev else 0.0),
            }
        return out

    # ---------- 对外 ----------
    def get_spot(self, force: bool = False) -> Dict[str, dict]:
        now = time.time()
        with self._lock:
            if not force and self._cache and now - self._ts < self._ttl:
                return self._cache
        try:
            data = self._parse(self._fetch())
        except Exception as e:            # 断连 → 回退旧缓存, 不阻断接口
            print(f"[MarketSpot] 全市场快照获取失败: {e}")
            with self._lock:
                return self._cache
        with self._lock:
            if data:
                self._cache = data
                self._ts = now
            return self._cache


market_spot = MarketSpot()
