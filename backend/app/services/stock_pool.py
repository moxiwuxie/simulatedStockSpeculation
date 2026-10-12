"""训练标的池 —— 分时可用性 + 涨停次数统计 + 按档加权抽样

背景:
  训练模块「随机抽取标的」原先只在 8 只候选里碰运气找「有分时数据」的个股,
  且完全不区分标的的涨停活跃度。本模块把两件事持久化下来, 形成一个可复用的候选池:

    1) 分时可用性 (has_intraday): 能否拉到真实 1 分钟数据 (停牌/退市股通常为空)
    2) 涨停次数 (limit_up_count): 最近 STAT_WINDOW 根日K中的涨停次数, 分 4 档

  抽样时「先按用户配置的权重定档, 再在档内均匀抽股」, 并支持避开最近几局已用档位,
  保证随机出来的个股涨停次数各不相同。

设计约束:
  - 所有对外函数异常安全: 任何失败都返回空/None, 由调用方回退到原有纯随机逻辑
  - 池存在注册表库 (data/trading.db) 的 stock_pool 表, 与 stocks 同库便于取名称/板块
  - 池规模靠「每次开训顺手写入 1 条」自然增长, 不做事先的全市场扫描
"""
from __future__ import annotations
import sqlite3
import time
from datetime import date
from pathlib import Path
from typing import Callable, Dict, List, Optional

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "trading.db"

# 涨停统计窗口 (根日K, 约一年)
STAT_WINDOW = 250
# 涨停判定容差 (消化前复权/四舍五入误差)
LIMIT_UP_TOL = 0.005
# 单次开训最多探测的候选数 (网络开销上界)
MAX_PROBE = 12
# 池内统计的保鲜期 (天), 超期允许重探
FRESH_DAYS = 1

# 涨停次数分档: key → (下界, 上界, 展示名)
BUCKETS = [
    ("none", 0, 0, "0 次"),
    ("low", 1, 2, "1-2 次"),
    ("mid", 3, 5, "3-5 次"),
    ("high", 6, 10 ** 9, "6+ 次"),
]
BUCKET_KEYS = [b[0] for b in BUCKETS]


# ---------- 涨停口径 ----------

def limit_up_pct(code: str, name: str = "", market: str = "") -> float:
    """涨停幅度: ST 5% / 北交所 30% / 科创板·创业板 20% / 其余 10%"""
    nm = (name or "").upper()
    if "ST" in nm:
        return 0.05
    if (market or "") == "bj":
        return 0.30
    c = str(code or "")
    if c.startswith(("688", "300")):
        return 0.20
    return 0.10


def count_limit_up(bars: List[dict], pct: float, window: int = STAT_WINDOW) -> int:
    """末尾 window 根内的涨停次数 (收盘相对前收盘涨幅 >= pct - 容差)"""
    if not bars or len(bars) < 2:
        return 0
    seg = bars[-window:] if window and len(bars) > window else bars
    cnt = 0
    for i in range(1, len(seg)):
        prev, cur = seg[i - 1].get("close"), seg[i].get("close")
        if not prev or not cur:
            continue
        if cur / prev - 1 >= pct - LIMIT_UP_TOL:
            cnt += 1
    return cnt


def bucket_of(count: int) -> str:
    for key, lo, hi, _label in BUCKETS:
        if lo <= count <= hi:
            return key
    return "none"


def bucket_label(key: str) -> str:
    for k, _lo, _hi, label in BUCKETS:
        if k == key:
            return label
    return ""


def bucket_meta() -> List[dict]:
    """供 /api/training/categories 返回给前端渲染权重控件"""
    return [{"key": k, "label": label} for k, _lo, _hi, label in BUCKETS]


# ---------- 成交量异常 ----------

def has_volume_anomaly(bars: List[dict], window: int = STAT_WINDOW,
                       ma_n: int = 20, mult: float = 3.0) -> bool:
    """末尾 window 根内是否存在「放量异常」: 某日成交量 >= mult × 前 ma_n 日均量"""
    if not bars or len(bars) < ma_n + 1:
        return False
    seg = bars[-window:] if window and len(bars) > window else bars
    vols = [float(b.get("volume") or 0) for b in seg]
    if len(vols) < ma_n + 1:
        return False
    run = sum(vols[:ma_n])
    for i in range(ma_n, len(vols)):
        ma = run / ma_n
        if ma > 0 and vols[i] >= mult * ma:
            return True
        run += vols[i] - vols[i - ma_n]
    return False



# ---------- 池表 ----------

def _db_path() -> Path:
    """测试可通过 patch 本函数注入临时库"""
    return DB_PATH


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path()))
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_table() -> None:
    with _conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS stock_pool (
            code TEXT PRIMARY KEY,
            name TEXT DEFAULT '',
            market TEXT DEFAULT '',
            board TEXT DEFAULT '',
            is_kcb INTEGER DEFAULT 0,
            bars_count INTEGER DEFAULT 0,
            limit_up_count INTEGER DEFAULT 0,
            limit_up_bucket TEXT DEFAULT '',
            vol_anomaly INTEGER DEFAULT 0,
            has_intraday INTEGER DEFAULT 0,
            last_close REAL DEFAULT 0,
            stat_date TEXT DEFAULT '',
            updated_at TEXT DEFAULT ''
        )""")
        # 迁移: 老库补齐 vol_anomaly (成交量异常标记)
        cols = {r[1] for r in c.execute("PRAGMA table_info(stock_pool)").fetchall()}
        if "vol_anomaly" not in cols:
            c.execute("ALTER TABLE stock_pool ADD COLUMN vol_anomaly INTEGER DEFAULT 0")
        c.execute("CREATE INDEX IF NOT EXISTS idx_stock_pool_bucket ON stock_pool(limit_up_bucket)")


def _today() -> str:
    return date.today().isoformat()


def update_stat(code: str, name: str = "", market: str = "", board: str = "",
                is_kcb: int = 0, bars: Optional[List[dict]] = None,
                has_intraday: bool = False) -> Optional[dict]:
    """用一次已拉到的 K 线写入/更新池中该标的的统计 (顺手写, 池自然增长)"""
    if not code or not bars:
        return None
    try:
        _ensure_table()
        pct = limit_up_pct(code, name, market)
        cnt = count_limit_up(bars, pct)
        bk = bucket_of(cnt)
        vol_anom = 1 if has_volume_anomaly(bars) else 0
        last_close = bars[-1].get("close") or 0
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        with _conn() as c:
            c.execute(
                """INSERT OR REPLACE INTO stock_pool
                   (code,name,market,board,is_kcb,bars_count,limit_up_count,limit_up_bucket,
                    vol_anomaly,has_intraday,last_close,stat_date,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (code, name, market, board, int(is_kcb or 0), len(bars), cnt, bk,
                 vol_anom, 1 if has_intraday else 0, float(last_close), _today(), now))
        return {"code": code, "limit_up_count": cnt, "limit_up_bucket": bk,
                "vol_anomaly": vol_anom,
                "limit_up_label": bucket_label(bk), "bars_count": len(bars)}
    except Exception as e:
        print(f"[StockPool] 写入统计失败 {code}: {e}")
        return None


def pool_stats(refresh_days: int = FRESH_DAYS) -> List[dict]:
    """读取池中「未过期」的行; stat_date 早于 N 天前视为过期 (不再纳入候选)"""
    try:
        _ensure_table()
        cutoff = date.fromordinal(date.today().toordinal() - max(0, refresh_days)).isoformat()
        with _conn() as c:
            rows = c.execute("SELECT * FROM stock_pool WHERE stat_date >= ?", (cutoff,)).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[StockPool] 读取池失败: {e}")
        return []


# ---------- 抽样 ----------

def choose_bucket(weights: Optional[List[float]], avoid: Optional[List[str]] = None) -> Optional[str]:
    """按权重加权随机定档; avoid 中的档权重置 0

    - 权重全为 0 / 全部被 avoid 排除 → 返回 None (语义: 不按档, 由调用方纯随机)
    - avoid 会把所有正权重档都排除时, 忽略 avoid 放开 (保证仍能抽到标的)
    """
    import random
    w = list(weights or [1, 1, 1, 1])
    if len(w) != len(BUCKET_KEYS):
        w = [1, 1, 1, 1]
    w = [max(0.0, float(x or 0)) for x in w]
    if sum(w) <= 0:
        return None
    av = set(avoid or [])
    if av:
        trimmed = [0.0 if BUCKET_KEYS[i] in av else w[i] for i in range(len(w))]
        if sum(trimmed) > 0:
            w = trimmed
    return random.choices(BUCKET_KEYS, weights=w, k=1)[0]


def candidates(bars_needed: int, only_intraday: bool = True) -> List[dict]:
    """池内可用候选 (历史足够长; 默认只取「确认有分时数据」的)"""
    out = []
    for r in pool_stats():
        if (r.get("bars_count") or 0) < bars_needed:
            continue
        if only_intraday and not r.get("has_intraday"):
            continue
        out.append(r)
    return out


def _registry_row(code: str) -> dict:
    try:
        from ..data.stock_registry import StockRegistry
        reg = StockRegistry()
        reg.ensure_ready()
        return reg.get(code) or {}
    except Exception:
        return {}


def _ledger_codes() -> List[str]:
    """注册表中可作为训练标的的代码 (沪深两市)"""
    try:
        from ..data.stock_registry import StockRegistry
        reg = StockRegistry()
        reg.ensure_ready()
        return [r["code"] for r in reg.list_all() if r.get("market") in ("sh", "sz")]
    except Exception:
        return []


def _stat_for(code: str, fetch_kline: Callable[[str], List[dict]],
              probe_intraday: bool = True) -> Optional[dict]:
    """拉一次日K → 统计涨停 + 探测分时 → 写池"""
    try:
        bars = fetch_kline(code)
    except Exception:
        return None
    if not bars:
        return None
    row = _registry_row(code)
    has_min = False
    if probe_intraday:
        try:
            from .intraday import has_minute_data
            has_min = bool(has_minute_data(code))
        except Exception:
            has_min = False
    info = update_stat(code, row.get("name") or "", row.get("market") or "",
                       row.get("board") or "", row.get("is_kcb") or 0,
                       bars=bars, has_intraday=has_min)
    if info is None:
        return None
    info.update({"market": row.get("market") or "", "board": row.get("board") or "",
                 "is_kcb": row.get("is_kcb") or 0, "has_intraday": 1 if has_min else 0,
                 "klines": bars})           # 复用本次已拉的 K 线, 避免二次请求
    return info


def pick_code(bars_needed: int, weights: Optional[List[float]] = None,
              avoid: Optional[List[str]] = None,
              fetch_kline: Optional[Callable[[str], List[dict]]] = None,
              max_probe: int = MAX_PROBE) -> Optional[dict]:
    """抽一只标的: 先定档 (池优先), 再档内抽股; 池不足时现场探测

    返回 {"code", "klines", "limit_up_count", "limit_up_bucket", "limit_up_label",
          "market", "board", "is_kcb"} ; 抽不到返回 None (调用方回退纯随机)
    """
    import random
    if not fetch_kline:
        from .training_session import _augment_amount, _fetch_stock_kline

        def fetch_kline(c):                              # type: ignore[misc]
            return _augment_amount(_fetch_stock_kline(c), "stock")

    target = choose_bucket(weights, avoid)
    pool = candidates(bars_needed, only_intraday=True)

    # ---- 1) 池内按档命中 (target=None 语义为「不按档」→ 池内全体随机) ----
    if pool:
        same = [r for r in pool if r.get("limit_up_bucket") == target] if target else pool
        if same:
            row = random.choice(same)
            try:
                klines = fetch_kline(row["code"])
            except Exception:
                klines = []
            if klines and len(klines) >= bars_needed:
                bk = row.get("limit_up_bucket") or bucket_of(row.get("limit_up_count") or 0)
                return {"code": row["code"], "klines": klines,
                        "limit_up_count": row.get("limit_up_count") or 0,
                        "limit_up_bucket": bk, "limit_up_label": bucket_label(bk),
                        "market": row.get("market") or "", "board": row.get("board") or "",
                        "is_kcb": row.get("is_kcb") or 0}
    # ---- 2) 池无目标档 → 现场探测 ----
    codes = _ledger_codes()
    if not codes:
        return None
    random.shuffle(codes)
    fallback: Optional[dict] = None
    for code in codes[:max_probe]:
        info = _stat_for(code, fetch_kline)
        if not info or info.get("bars_count", 0) < bars_needed:
            continue
        info["limit_up_label"] = bucket_label(info.get("limit_up_bucket") or "")
        if target and info.get("limit_up_bucket") == target:
            return info
        if fallback is None and info.get("has_intraday"):
            fallback = info
    return fallback


def pick_tiered(bars_needed: int, exclude: Optional[List[str]] = None,
                fetch_kline: Optional[Callable[[str], List[dict]]] = None,
                max_probe: int = MAX_PROBE) -> Optional[dict]:
    """分级优先抽标 (权重逻辑内部化, 不再由用户配置)

    优先级 (逐级放宽):
      T1 周期内有涨停次数的个股 (limit_up_count > 0)
      T2 周期内存在成交量异常的个股 (vol_anomaly = 1)
      T3 取消内容过滤, 按「统计日/收盘日距离当前最近」优先

    层内「距离当前时间最近」优先 (stat_date 最新的一批), 并列随机;
    始终避开 exclude (同账号最近 N 局已用代码), 层内被排空时自动放宽到下一层。
    返回结构与 pick_code 一致; 抽不到返回 None (调用方回退纯随机)。
    """
    import random
    if not fetch_kline:
        from .training_session import _augment_amount, _fetch_stock_kline

        def fetch_kline(c):                              # type: ignore[misc]
            return _augment_amount(_fetch_stock_kline(c), "stock")
    ex = set(exclude or [])

    def _as_hit(row: dict, klines: List[dict]) -> dict:
        bk = row.get("limit_up_bucket") or bucket_of(row.get("limit_up_count") or 0)
        return {"code": row["code"], "klines": klines,
                "limit_up_count": row.get("limit_up_count") or 0,
                "limit_up_bucket": bk, "limit_up_label": bucket_label(bk),
                "market": row.get("market") or "", "board": row.get("board") or "",
                "is_kcb": row.get("is_kcb") or 0}

    pool = candidates(bars_needed, only_intraday=True)
    # 最近优先: stat_date 降序 (同日再按 last_close 降序, 稳定排序)
    pool.sort(key=lambda r: (r.get("stat_date") or "", r.get("last_close") or 0), reverse=True)
    tiers = (
        [r for r in pool if (r.get("limit_up_count") or 0) > 0],   # T1
        [r for r in pool if r.get("vol_anomaly")],                 # T2
        list(pool),                                                # T3 全量
    )
    for tier in tiers:
        cands = [r for r in tier if r.get("code") not in ex]
        if not cands:
            continue
        # 层内最近优先: 取 stat_date 最新的一批, 并列随机
        best_date = cands[0].get("stat_date") or ""
        near = [r for r in cands if (r.get("stat_date") or "") == best_date]
        random.shuffle(near)
        for row in near:
            try:
                klines = fetch_kline(row["code"])
            except Exception:
                klines = []
            if klines and len(klines) >= bars_needed:
                return _as_hit(row, klines)

    # ---- 池不足 → 现场探测 (T1 → T2 → T3 逐级放宽) ----
    codes = [c for c in _ledger_codes() if c not in ex]
    if not codes:
        codes = _ledger_codes()          # exclude 全排空: 放开去重
    if not codes:
        return None
    random.shuffle(codes)
    t1 = t2 = t3 = None
    for code in codes[:max_probe]:
        info = _stat_for(code, fetch_kline)
        if not info or info.get("bars_count", 0) < bars_needed:
            continue
        info["limit_up_label"] = bucket_label(info.get("limit_up_bucket") or "")
        if (info.get("limit_up_count") or 0) > 0 and t1 is None:
            t1 = info
        elif info.get("vol_anomaly") and t2 is None:
            t2 = info
        elif info.get("has_intraday") and t3 is None:
            t3 = info
        if t1 is not None:
            return t1
    return t2 or t3

