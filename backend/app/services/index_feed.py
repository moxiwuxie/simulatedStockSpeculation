"""大盘指数行情 - 看盘点击指数跳转图表模块所需数据源

数据源: 新浪日K (akshare.stock_zh_index_daily, 稳定), 10 分钟内存缓存。

- list_indices(): 常用大盘指数清单 [{code, name, symbol}]
- index_kline(code, days, period): 指数 K 线 (日/周/月/年), 与个股/板块 K 线返回结构一致
- index_intraday(code, date): 指数分时 (真实 1 分钟优先, 缺失时 OHLC 确定性重建)

indices 代码与看盘模块 (services/watch.py) 保持一致, 前端点击后可无缝跳转。
"""
from __future__ import annotations
import time
from datetime import datetime as _dt, date as _date
from typing import Dict, List, Optional

_TTL = 600          # 缓存 10 分钟
_CACHE: Dict[str, tuple] = {}
_MIN_TTL = 900      # 指数分钟序列缓存 15 分钟
_MIN_CACHE: Dict[str, tuple] = {}

# (名称, 代码, 新浪符号) —— 与看盘模块指数条一一对应
INDEXES = [
    ("上证指数", "000001", "sh000001"),
    ("深证成指", "399001", "sz399001"),
    ("创业板指", "399006", "sz399006"),
    ("科创50", "000688", "sh000688"),
    ("沪深300", "000300", "sh000300"),
    ("北证50", "899050", "bj899050"),
]


def _cache_get(key: str):
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < _TTL:
        return hit[1]
    return None


def _cache_set(key: str, value):
    _CACHE[key] = (time.time(), value)


def list_indices() -> List[dict]:
    """大盘指数清单 [{code, name, symbol}]"""
    return [{"code": c, "name": n, "symbol": s} for n, c, s in INDEXES]


def _meta(code: str):
    """代码 → (name, symbol); 兼容传入符号 (如 'sh000001')"""
    for n, c, s in INDEXES:
        if code in (c, s):
            return n, s
    return code, code


def _daily_klines(symbol: str):
    """指数日K (缓存 10 分钟), 返回 KLine 列表 (amount 新浪不提供, 置 0)"""
    key = f"idx|{symbol}"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    from ..models import KLine
    import akshare as ak
    df = ak.stock_zh_index_daily(symbol=symbol)
    out = []
    if df is not None and not df.empty:
        for _, r in df.iterrows():
            ts = str(r.get("date"))
            try:
                d = _dt.strptime(ts[:10], "%Y-%m-%d")
                out.append(KLine(
                    open=float(r.get("open") or 0), high=float(r.get("high") or 0),
                    low=float(r.get("low") or 0), close=float(r.get("close") or 0),
                    volume=int(float(r.get("volume") or 0)), amount=0.0,
                    timestamp=d,
                ))
            except (TypeError, ValueError):
                continue
    out.sort(key=lambda k: k.timestamp)
    _cache_set(key, out)
    return out


def index_kline(code: str, days: int = 240, period: str = "day") -> List[dict]:
    """指数 K 线 (结构同个股: [{date, open, high, low, close, volume, amount, change_pct}])"""
    from .replay_feed import ReplayFeed
    _name, symbol = _meta(code)
    kl = _daily_klines(symbol)
    if not kl:
        return []
    agg = ReplayFeed.aggregate(kl, period)
    out, prev = [], None
    for k in agg[-days:]:
        out.append({
            "date": k.timestamp.strftime("%Y-%m-%d"),
            "open": round(k.open, 2), "high": round(k.high, 2),
            "low": round(k.low, 2), "close": round(k.close, 2),
            "volume": int(k.volume), "amount": k.amount,
            "change_pct": round((k.close - prev) / prev * 100, 2) if prev else None,
        })
        prev = k.close
    return out


def index_name(code: str) -> str:
    """指数名称 (兼容代码/符号)"""
    return _meta(code)[0]


def index_quote(code: str) -> dict:
    """需求101506-2: 指数最新报价 (供自选/概览展示)"""
    _name, symbol = _meta(code)
    kl = _daily_klines(symbol)
    if not kl:
        return {"price": None, "prev_close": None, "change_pct": None,
                "open": None, "high": None, "low": None, "volume": 0, "suspended": True}
    k = kl[-1]
    prev = kl[-2].close if len(kl) > 1 else k.open
    return {
        "price": round(k.close, 2), "prev_close": round(prev, 2),
        "change_pct": round((k.close - prev) / prev * 100, 2) if prev else 0.0,
        "open": round(k.open, 2), "high": round(k.high, 2), "low": round(k.low, 2),
        "volume": int(k.volume), "suspended": int(k.volume) == 0,
    }


def _index_minute_series(symbol: str) -> Dict[str, List[tuple]]:
    """指数最近真实 1 分钟序列, 按交易日分组 {day: [(hhmm, price, volume)]}"""
    now = time.time()
    hit = _MIN_CACHE.get(symbol)
    if hit and now - hit[0] < _MIN_TTL:
        return hit[1]
    days: Dict[str, List[tuple]] = {}
    try:
        import akshare as ak
        df = ak.stock_zh_a_minute(symbol=symbol, period="1")
        if df is not None and not df.empty:
            for _, row in df.iterrows():
                ts = str(row.get("day"))
                if len(ts) < 16:
                    continue
                try:
                    price = float(row.get("close"))
                    vol = max(1, int(float(row.get("volume") or 0)))
                except (TypeError, ValueError):
                    continue
                days.setdefault(ts[:10], []).append((ts[11:16], price, vol))
    except Exception as e:
        print(f"[IndexFeed] 指数分钟拉取失败 {symbol}: {e}")
        days = {}
    for d in days:
        days[d].sort(key=lambda x: x[0])
    _MIN_CACHE[symbol] = (now, days)
    return days


def index_intraday(code: str, date: Optional[str] = None) -> dict:
    """指数分时: 真实 1 分钟优先, 不可得时按当日 OHLC 确定性重建

    - 不传 date: 取指数最新交易日
    - 传 date=YYYY-MM-DD: 查看该日分时 (K线点击联动)
    """
    from .intraday import generate_intraday
    _name, symbol = _meta(code)
    kl = _daily_klines(symbol)
    if not kl:
        raise ValueError("该指数暂无 K 线数据")

    if date:
        try:
            day = _dt.strptime(date, "%Y-%m-%d").date()
        except ValueError:
            raise ValueError("date 格式应为 YYYY-MM-DD")
    else:
        # 需求101601: 当日已有真实分钟数据时优先「当日」(指数日K同样收盘后才更新)
        _today = _date.today()
        day = _today if (_index_minute_series(symbol).get(str(_today)) or []) else kl[-1].timestamp.date()

    # 定位该日 K 线 + 前一交易日收盘 (分时基准)
    cur_idx = None
    for i, k in enumerate(kl):
        if k.timestamp.date() == day:
            cur_idx = i
            break
    if cur_idx is None:
        # 需求101601: 当日指数日K未更新但有真实分钟数据 → 用分钟序列构造当日分时
        _pts = _index_minute_series(symbol).get(str(day)) or []
        if _pts:
            from .intraday import build_intraday_from_minutes
            data = build_intraday_from_minutes(
                day, _pts, kl[-1].close,
                {"is_index": True, "is_today": day == _date.today()})
            if data:
                return data
        # 该日无 K 线 (休市/未上市): 返回空数据样式
        return {"date": str(day), "prev_close": None, "points": [],
                "suspended": False, "empty": True, "is_index": True, "source": "empty",
                "hint": "该日无指数行情数据"}
    k = kl[cur_idx]
    prev_close = kl[cur_idx - 1].close if cur_idx > 0 else None

    # 真实分钟优先
    pts = _index_minute_series(symbol).get(str(day)) or []
    if pts:
        points, cum_v, cum_pv = [], 0, 0.0
        for t, p, v in pts:
            cum_v += v
            cum_pv += p * v
            points.append({"time": t, "price": round(p, 2),
                           "avg": round(cum_pv / cum_v, 2), "volume": v})
        data = {
            "date": str(day), "prev_close": prev_close,
            "open": round(k.open, 2), "high": round(k.high, 2),
            "low": round(k.low, 2), "close": round(k.close, 2),
            "amplitude": round((k.high - k.low) / prev_close * 100, 2) if prev_close else None,
            "suspended": False, "is_index": True, "source": "real", "points": points,
        }
    else:
        data = generate_intraday("idx:" + symbol, day, k.open, k.high, k.low, k.close,
                                 k.volume, prev_close)
        data["is_index"] = True
        data["source"] = "index-recon"

    data["is_today"] = (day == _date.today())
    return data
