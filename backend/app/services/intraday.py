"""分时数据合成 - 真实分钟优先, OHLC 布朗桥重建兜底

数据流:
  1) generate_intraday_real(): 拉取新浪 1 分钟真实分时 (带内存缓存), 命中该日才返回
  2) generate_intraday():       失败/停牌时用真实 OHLC 确定性重建 240 分钟, 保证不空窗
返回结构一致: {date, prev_close, open/high/low/close, amplitude, suspended, points:[{time,price,avg,volume}]}

数据源限制: 新浪 1 分钟接口仅提供最近约 8 个交易日的分钟数据, 更早的日期无法取得真实分时,
此时由调用方决定是否回退 (训练模块会提示「该日无真实分钟数据」)。
"""
from __future__ import annotations
import random
import time
from typing import Optional

# 个股 1 分钟序列缓存: code → (时间戳, {date: [(time, price, volume), ...]})
_MIN_SERIES_CACHE = {}
_MIN_SERIES_TTL = 900  # 15 分钟

# 真实分钟数据缓存: key=f"{code}|{day}" → data | None (None 表示已确认拉不到)
_MIN_REAL_CACHE = {}
_MIN_REAL_TTL = 1200  # 20 分钟


def _sina_symbol(code: str) -> str:
    """A 股代码 → 新浪符号 (6/9 开头为沪市, 其余深市/北交所按深市处理)"""
    return ("sh" if str(code).startswith(("6", "9")) else "sz") + str(code)


def minute_series(code: str) -> dict:
    """该股最近的真实 1 分钟序列, 按交易日分组: {date: [(time, price, volume), ...]}

    数据源为新浪 (akshare.stock_zh_a_minute), 仅覆盖最近约 8 个交易日。
    拉取失败/无数据返回 {} (调用方回退)。
    """
    now = time.time()
    hit = _MIN_SERIES_CACHE.get(code)
    if hit and now - hit[0] < _MIN_SERIES_TTL:
        return hit[1]
    days: dict = {}
    try:
        import akshare as ak
        df = ak.stock_zh_a_minute(symbol=_sina_symbol(code), period="1")
        if df is not None and not df.empty:
            for _, row in df.iterrows():
                ts = str(row.get("day"))
                if len(ts) < 16:
                    continue
                day, hhmm = ts[:10], ts[11:16]
                try:
                    price = float(row.get("close"))
                    vol = max(1, int(float(row.get("volume") or 0)))
                except (TypeError, ValueError):
                    continue
                days.setdefault(day, []).append((hhmm, price, vol))
    except Exception as e:
        print(f"[Intraday] 新浪分钟拉取失败 {code}: {e}")
        days = {}
    for d in days:
        days[d].sort(key=lambda x: x[0])
    _MIN_SERIES_CACHE[code] = (now, days)
    return days


def has_minute_data(code: str) -> bool:
    """该标的是否有真实分钟数据 (用于训练标的筛选)"""
    return bool(minute_series(code))


def _minute_label(i: int) -> str:
    """i: 0..239 → '09:31'..'11:30', '13:01'..'15:00'"""
    if i < 120:
        total = 9 * 60 + 31 + i
    else:
        total = 13 * 60 + 1 + (i - 120)
    return f"{total // 60:02d}:{total % 60:02d}"


def generate_intraday_real(code: str, day, open_: float, high: float, low: float,
                           close: float, volume: int, prev_close: Optional[float]) -> Optional[dict]:
    """取该日真实 1 分钟分时; 无数据返回 None (调用方回退布朗桥重建)"""
    cache_key = f"{code}|{day}"
    now = time.time()
    hit = _MIN_REAL_CACHE.get(cache_key)
    if hit and now - hit[0] < _MIN_REAL_TTL:
        return hit[1]

    pts = minute_series(code).get(str(day)) or []
    if not pts:
        _MIN_REAL_CACHE[cache_key] = (now, None)
        return None

    # 均价线 (累计 VWAP)
    points, cum_v, cum_pv = [], 0, 0.0
    for t, p, v in pts:
        cum_v += v
        cum_pv += p * v
        points.append({"time": t, "price": round(p, 2),
                       "avg": round(cum_pv / cum_v, 2), "volume": v})
    if not points:
        _MIN_REAL_CACHE[cache_key] = (now, None)
        return None
    data = {
        "date": str(day), "prev_close": prev_close,
        "open": round(open_, 2), "high": round(high, 2), "low": round(low, 2), "close": round(close, 2),
        "amplitude": round((high - low) / prev_close * 100, 2) if prev_close else None,
        "suspended": False, "source": "real", "points": points,
    }
    _MIN_REAL_CACHE[cache_key] = (now, data)
    return data


def generate_intraday(code: str, day, open_: float, high: float, low: float,
                      close: float, volume: int, prev_close: Optional[float]) -> dict:
    """合成某日分时。返回 {date, prev_close, suspended, open/high/low/close, amplitude, points:[...]}"""
    base = {
        "date": str(day), "prev_close": prev_close,
        "open": round(open_, 2), "high": round(high, 2), "low": round(low, 2), "close": round(close, 2),
        "amplitude": round((high - low) / prev_close * 100, 2) if prev_close else None,
        "suspended": volume == 0, "points": [],
    }
    n = 240
    if not open_ or open_ <= 0 or high is None or low is None:
        return base
    if volume == 0:  # 停牌: 平线在昨收
        for i in range(n):
            p = prev_close or close
            base["points"].append({"time": _minute_label(i), "price": p, "avg": p, "volume": 0})
        return base

    # ---- 布朗桥路径 (种子确定性) ----
    seed = int(f"{code}{day}".replace('-', '')[-9:]) if str(code).isdigit() else 42
    rng = random.Random(seed)
    walk = [0.0]
    for _ in range(n - 1):
        walk.append(walk[-1] + rng.gauss(0, 1))
    end = walk[-1] if walk[-1] != 0 else 1.0
    bridge = [w - (t / (n - 1)) * end for t, w in enumerate(walk)]  # 两端归零
    bmax, bmin = max(bridge), min(bridge)
    brange = (bmax - bmin) or 1.0

    span = high - low
    trend = close - open_
    path = []
    for i in range(n):
        dev = (bridge[i] - bmin) / brange - 0.5      # [-0.5, 0.5], 两端≈±0.5 附近
        p = open_ + trend * (i / (n - 1)) + dev * span
        path.append(min(high, max(low, p)))
    path[0] = open_
    path[-1] = close
    # 保证触顶/触底
    path[path.index(max(path))] = high
    path[path.index(min(path))] = low

    # ---- 分钟成交量: U型分布 (开盘/收盘活跃) ----
    weights = [1.0 + 1.6 * ((2 * i / (n - 1)) - 1) ** 2 + rng.random() * 0.3 for i in range(n)]
    wsum = sum(weights)
    vols = [max(1, int(volume * w / wsum)) for w in weights]

    # ---- 均价线 (累计 VWAP) ----
    points, cum_v, cum_pv = [], 0, 0.0
    for i in range(n):
        cum_v += vols[i]
        cum_pv += path[i] * vols[i]
        points.append({
            "time": _minute_label(i),
            "price": round(path[i], 2),
            "avg": round(cum_pv / cum_v, 2),
            "volume": vols[i],
        })
    base["points"] = points
    return base
