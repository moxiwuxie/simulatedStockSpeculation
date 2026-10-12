"""板块行情 - 新浪板块查询 (行业/概念) + 成分股

- list_boards(kind): 板块列表 (industry=行业 / concept=概念)
- board_stocks(kind, label): 板块成分股 (label 为板块唯一标识)
带 10 分钟内存缓存。数据源: 新浪 (本项目已验证稳定)。
"""
from __future__ import annotations
import time
from datetime import datetime as _dt
from typing import Optional, List, Dict

_TTL = 600          # 缓存 10 分钟
_CACHE: Dict[str, tuple] = {}


def _cache_get(key: str):
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < _TTL:
        return hit[1]
    return None


def _cache_set(key: str, value):
    _CACHE[key] = (time.time(), value)


def _f(v) -> Optional[float]:
    """DataFrame 单元格 → float (nan/None → None)"""
    if v is None:
        return None
    import math
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def _s(v) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def list_boards(kind: str = "industry") -> List[dict]:
    """板块列表 [{label, name, pct, leader, leader_pct}] (新浪, 含分类标识)"""
    key = f"boards|{kind}"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    import akshare as ak
    indicator = "行业" if kind == "industry" else "概念"
    df = ak.stock_sector_spot(indicator=indicator)
    rows = []
    for _, r in df.iterrows():
        rows.append({
            "label": _s(r.get("label")),
            "name": _s(r.get("板块")),
            "pct": _f(r.get("涨跌幅")),
            "leader": _s(r.get("股票名称")),
            "leader_pct": _f(r.get("个股-涨跌幅")),
        })
    _cache_set(key, rows)
    return rows


def search_boards(boards: List[dict], kw: str, kind: str, limit: int = 12) -> List[dict]:
    """板块模糊搜索: 名称 / 拼音首字母 / 全拼 (供聚合搜索接口使用)

    输入为 list_boards() 的结果, 命中项附加 {kind, prefix_hit}
    """
    from ..data.stock_registry import pinyin_of
    text = (kw or "").strip().lower()
    if not text:
        return []
    out = []
    for b in boards:
        name = b.get("name") or ""
        py = pinyin_of(name)
        if text not in name.lower() and text not in py and text not in (b.get("label") or "").lower():
            continue
        out.append({**b, "kind": kind,
                    "prefix_hit": name.lower().startswith(text) or py.startswith(text)})
        if len(out) >= limit:
            break
    return out


def search_all_boards(kw: str, limit: int = 20) -> List[dict]:
    """跨「行业 + 概念」模糊搜索板块 (名称 / 拼音 / label), 前缀命中优先

    供行情页板块面板内的搜索框使用 (同花顺风格: 一次搜行业+概念)。
    """
    out: List[dict] = []
    for kind in ("industry", "concept"):
        try:
            out += search_boards(list_boards(kind), kw, kind, limit)
        except Exception:
            continue
    out.sort(key=lambda b: (not b.get("prefix_hit"), b.get("name") or ""))
    return out[:limit]


def board_stocks(kind: str, label: str) -> List[dict]:
    """成分股 [{code, name, price, pct, nmc, turnover}] (新浪板块详情, 按涨跌幅正序)"""
    key = f"cons|{kind}|{label}"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    import akshare as ak
    df = ak.stock_sector_detail(sector=label)
    out = []
    for _, r in df.iterrows():
        out.append({
            "code": _s(r.get("code")),
            "name": _s(r.get("name")),
            "price": _f(r.get("trade")),
            "pct": _f(r.get("changepercent")),
            "nmc": _f(r.get("nmc")),                  # 流通市值 (元) — 板块指数加权用
            "turnover": _f(r.get("turnoverratio")),
        })
    out.sort(key=lambda x: (x["pct"] is None, x["pct"] if x["pct"] is not None else 0))  # 涨跌幅正序
    _cache_set(key, out)
    return out


# ============================================================
# 板块 K 线 / 分时 —— 成分股复合指数 (基准 1000)
# 数据源: 新浪日K (feed.get_kline) / 新浪 1 分钟 (仅最近约 5 个交易日)
# ============================================================

_INDEX_N = 20        # 合成指数的成分股数量上限 (按流通市值降序取前 N)
_MINUTE_N = 10       # 合成分钟线的成分股数量上限


def _top_codes(cons: List[dict], n: int) -> List[str]:
    """按流通市值降序取前 n 只成分股代码 (市值缺失则退化为原始顺序)"""
    ranked = sorted(cons, key=lambda x: (x.get("nmc") or 0), reverse=True)
    return [c["code"] for c in ranked if c.get("code")][:n]


def board_index(feed, kind: str, label: str, days: int = 240, period: str = "day") -> List[dict]:
    """板块 K 线: 成分股(前 N 只) 等权复合指数, 基准 1000

    返回结构与个股 K 线一致: [{date, open, high, low, close, volume, amount, change_pct}]
    """
    agg = _board_index_full(feed, kind, label, period)
    out, prev = [], None
    for k in agg[-days:]:
        out.append({
            "date": k.timestamp.strftime("%Y-%m-%d"),
            "open": round(k.open, 2), "high": round(k.high, 2),
            "low": round(k.low, 2), "close": round(k.close, 2),
            "volume": int(k.volume), "amount": round(k.amount, 2),
            "change_pct": round((k.close - prev) / prev * 100, 2) if prev else None,
        })
        prev = k.close
    return out


def _board_index_full(feed, kind: str, label: str, period: str = "day"):
    """板块复合指数全量 KLine (缓存 10 分钟; 不随 days 变化, 便于复用)"""
    key = f"bidx|{kind}|{label}|{period}"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    from .replay_feed import ReplayFeed
    cons = board_stocks(kind, label)
    codes = _top_codes(cons, _INDEX_N)
    series = _fetch_daily_series(feed, codes)
    klines = _composite_index(series, base=1000.0) if series else []
    agg = ReplayFeed.aggregate(klines, period) if klines else []
    _cache_set(key, agg)
    return agg


def _fetch_daily_series(feed, codes: List[str]) -> Dict[str, Dict[str, dict]]:
    """并发拉取若干股票的日K, 返回 {code: {date_str: {open,high,low,close,volume,amount}}}"""
    from concurrent.futures import ThreadPoolExecutor

    def one(code):
        try:
            kl = feed.get_kline(code)
        except Exception:
            return code, {}
        m = {}
        for k in kl:
            m[k.timestamp.strftime("%Y-%m-%d")] = {
                "open": k.open, "high": k.high, "low": k.low,
                "close": k.close, "volume": k.volume, "amount": k.amount,
            }
        return code, m

    result: Dict[str, Dict[str, dict]] = {}
    if not codes:
        return result
    with ThreadPoolExecutor(max_workers=8) as ex:
        for code, m in ex.map(one, codes):
            if m:
                result[code] = m
    return result


def _composite_index(series: Dict[str, Dict[str, dict]], base: float = 1000.0):
    """多股票 → 等权复合指数 K 线 (以各股自身首日收盘归一化)"""
    from datetime import datetime
    from ..models import KLine

    dates = set()
    for m in series.values():
        dates |= set(m.keys())
    dates = sorted(dates)
    if not dates:
        return []
    # 只保留多数股票都有的交易日 (避免个别停牌导致指数跳空)
    need = max(1, int(len(series) * 0.5))
    valid = [d for d in dates if sum(1 for m in series.values() if d in m) >= need]
    if not valid:
        return []
    anchor = valid[0]
    norm = {}   # code → 锚点收盘价
    for code, m in series.items():
        if anchor in m and m[anchor]["close"]:
            norm[code] = m[anchor]["close"]

    klines = []
    for d in valid:
        rows = [(code, m[d]) for code, m in series.items() if code in norm and d in m]
        if not rows:
            continue
        n = len(rows)
        o = sum(r["open"] / norm[c] for c, r in rows) / n
        h = sum(r["high"] / norm[c] for c, r in rows) / n
        l = sum(r["low"] / norm[c] for c, r in rows) / n
        cl = sum(r["close"] / norm[c] for c, r in rows) / n
        vol = sum(r["volume"] for _, r in rows)
        amt = sum(r["amount"] for _, r in rows)
        dt = datetime.strptime(d, "%Y-%m-%d")
        klines.append(KLine(open=o * base, high=h * base, low=l * base, close=cl * base,
                            volume=vol, amount=amt,
                            timestamp=datetime(dt.year, dt.month, dt.day)))
    return klines


def _sina_symbol(code: str) -> str:
    if code.startswith(("6", "9")):
        return "sh" + code
    if code.startswith(("0", "3")):
        return "sz" + code
    return "bj" + code


def board_intraday(feed, kind: str, label: str, date: Optional[str] = None) -> dict:
    """板块分时: 成分股(前 N 只)真实 1 分钟按成交量加权合成

    - 每只成分股先转成「相对自身昨收的涨跌幅」(复权无关), 再按成交量加权
    - 最后映射到板块指数点位 (prev_close = 板块指数在 target 前一交易日收盘)
    - 新浪分钟仅覆盖最近约 5 个交易日, 更早日期返回 empty 结构 (前端展示空数据样式)
    """
    cons = board_stocks(kind, label)
    codes = _top_codes(cons, _MINUTE_N)
    frames = _fetch_minute(codes)                # {code: [{day,time,price,volume}, ...]}
    if not frames:
        return _empty_intraday(date, "该板块暂无真实分钟数据")

    all_days = sorted({r["day"] for rows in frames.values() for r in rows})
    # 指定历史日期但该日无真实分钟数据: 明确返回空结构, 不做静默回退
    # (否则点击历史K线会展示「最新交易日」的分时, 与所选日期不符)
    if date:
        if date not in all_days:
            return _empty_intraday(date, "该日无真实分钟数据, 仅展示板块日线")
        target = date
    else:
        target = all_days[-1]
    day_rows = {c: [r for r in rows if r["day"] == target] for c, rows in frames.items()}
    day_rows = {c: rows for c, rows in day_rows.items() if rows}
    if not day_rows:
        return _empty_intraday(target, "该日无真实分钟数据, 仅展示板块日线")

    # 各股昨收 (取该日之前最近一根日K收盘), 用于计算当日涨跌幅
    pc_map = {}
    for c in day_rows:
        try:
            prev = feed._prev_kline(c, _dt.strptime(target, "%Y-%m-%d").date())
        except Exception:
            prev = None
        if prev and prev.close:
            pc_map[c] = prev.close

    # 逐分钟: 成交量加权涨跌幅
    times, pcts, vols = [], [], []
    for i in range(max(len(r) for r in day_rows.values())):
        pv, pv_pct, t = 0.0, 0.0, None
        for c, rows in day_rows.items():
            if i >= len(rows) or c not in pc_map:
                continue
            r = rows[i]
            t = t or r["time"]
            pv += r["volume"]
            pv_pct += r["volume"] * (r["price"] / pc_map[c] - 1)
        if t is None or pv <= 0:
            continue
        times.append(t)
        pcts.append(pv_pct / pv)
        vols.append(int(pv))

    if not times:
        return _empty_intraday(target, "该日无真实分钟数据, 仅展示板块日线")

    # 板块指数基准点位 (target 前一交易日收盘)
    idx_agg = _board_index_full(feed, kind, label, "day")
    prev_close = 1000.0
    for k in idx_agg:
        d = k.timestamp.strftime("%Y-%m-%d")
        if d < target:
            prev_close = round(k.close, 2)
        else:
            break
    prices = [round(prev_close * (1 + p), 2) for p in pcts]

    points, cum_v, cum_pv = [], 0, 0.0
    for i in range(len(prices)):
        cum_v += vols[i]
        cum_pv += prices[i] * vols[i]
        points.append({"time": times[i], "price": prices[i],
                       "avg": round(cum_pv / cum_v, 2), "volume": vols[i]})
    return {
        "date": target, "prev_close": prev_close,
        "open": prices[0], "high": max(prices), "low": min(prices), "close": prices[-1],
        "amplitude": round((max(prices) - min(prices)) / prev_close * 100, 2) if prev_close else None,
        "suspended": False, "source": "board-real", "is_board": True, "points": points,
        "minutes_available_days": all_days[-5:],
    }


def _empty_intraday(date, hint: str) -> dict:
    return {"date": str(date) if date else None, "prev_close": None, "points": [],
            "suspended": False, "empty": True, "is_board": True, "source": "empty", "hint": hint}


def _fetch_minute(codes: List[str]) -> Dict[str, List[dict]]:
    """并发拉取新浪 1 分钟数据 (仅最近约 5 个交易日)"""
    from concurrent.futures import ThreadPoolExecutor

    def one(code):
        try:
            import akshare as ak
            df = ak.stock_zh_a_minute(symbol=_sina_symbol(code), period="1")
        except Exception:
            return code, []
        rows = []
        if df is None or df.empty:
            return code, rows
        for _, r in df.iterrows():
            ts = str(r.get("day"))
            try:
                v = float(r.get("volume") or 0)
            except (TypeError, ValueError):
                v = 0.0
            try:
                p = float(r.get("close") or 0)
            except (TypeError, ValueError):
                p = 0.0
            rows.append({"day": ts[:10], "time": ts[11:16], "price": p, "volume": max(v, 1.0)})
        return code, rows

    out: Dict[str, List[dict]] = {}
    if not codes:
        return out
    with ThreadPoolExecutor(max_workers=8) as ex:
        for code, rows in ex.map(one, codes):
            if rows:
                out[code] = rows
    return out