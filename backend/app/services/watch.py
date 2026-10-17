"""真实看盘 (聚合看板) —— 同花顺 iFinD 数据接入

聚合看盘数据形成自有看盘界面结构, 对外统一由 ``real_panorama()`` 提供:

- 已配置 iFinD 密钥时逐板块取真实数据:
    * indices        -> index_highfreq_quotes (一次请求同时取指数快照与市场宽度, 沪深两市家数相加)
    * foreign        -> index_data 自然语言查询 (日经225/韩国综合/道琼斯/纳斯达克/标普500)
    * breadth        -> 复用 indices 的同一次 index_highfreq_quotes
    * hot_boards     -> 新浪行业板块快照 (实时涨跌幅/领涨股) + sector_data 净主动买入额
    * hot_stocks     -> search_stocks 涨幅前列 (异动个股, 按涨跌幅降序)
    * limit_up_today / limit_up_prev / limit_down / zhaban -> search_stocks 条件查询
- 未配置密钥、网关认证失败或任一段解析失败时, **仅该段** 回落 ``_section_*`` 占位数据,
  保证接口始终返回完整结构、永不 5xx;
- 返回体中的 ``mode`` (live/reserve)、``live_sections``、``errors`` 供前端与运维定位。

``mock_panorama()`` 保留为全量占位快照 (供离线联调/降级兜底)。
"""
from __future__ import annotations
import random
import threading
import time
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from . import ifind_client

# 数据来源
SOURCES = ("同花顺", "开盘啦")
LIVE_SOURCE = "同花顺 iFinD"

# 看盘界面覆盖的板块 (顺序即前端展示顺序)
SECTIONS = (
    "indices", "foreign", "breadth", "hot_boards", "hot_stocks",
    "limit_up_today", "limit_up_prev", "limit_down", "zhaban",
)

_INDEX_LIST = [
    ("上证指数", "000001"), ("深证成指", "399001"), ("创业板指", "399006"),
    ("科创50", "000688"), ("沪深300", "000300"), ("北证50", "899050"),
]

# 日韩美股指数 (展示名, iFinD 主体代码)
_FOREIGN_MAP: List[Tuple[str, str, str]] = [
    ("日经225", "N225.GI", "日经225"),
    ("韩国综合", "700648.TI", "韩国综合"),
    ("道琼斯", "DJI.GI", "道琼斯"),
    ("纳斯达克", "IXIC.GI", "纳斯达克"),
    ("标普500", "SPX.GI", "标普500"),
]

_FOREIGN_QUERY = "日经225指数、韩国综合指数、道琼斯工业指数、纳斯达克指数、标普500指数的点位和涨跌幅"

_BOARD_POOL = [
    "半导体", "人工智能", "固态电池", "机器人", "算力", "CPO", "光伏", "军工",
    "创新药", "白酒", "券商", "低空经济", "数据要素", "卫星互联网", "稀土永磁",
]

_STOCK_POOL = [
    ("贵州茅台", "600519"), ("宁德时代", "300750"), ("中芯国际", "688981"),
    ("比亚迪", "002594"), ("东方财富", "300059"), ("隆基绿能", "601012"),
    ("中国软件", "600536"), ("寒武纪", "688256"), ("拓维信息", "002261"),
    ("剑桥科技", "603083"), ("工业富联", "601138"), ("科大讯飞", "002230"),
]


def _rng(seed: Optional[int] = None) -> random.Random:
    """确定性随机源: 同一天返回同一份快照 (便于前端联调与截图复现)"""
    return random.Random(seed if seed is not None else date.today().toordinal())


def _section_indices(r: random.Random) -> List[Dict]:
    out = []
    for name, code in _INDEX_LIST:
        prev = round(r.uniform(900, 3500), 2)
        pct = round(r.uniform(-2.5, 2.5), 2)
        out.append({
            "name": name, "code": code,
            "price": round(prev * (1 + pct / 100), 2),
            "pct": pct,
            "amount": round(r.uniform(500, 9000) * 1e8, 2),   # 成交额 (元)
        })
    return out


def _section_foreign(r: random.Random) -> List[Dict]:
    """日韩美股指数占位快照"""
    base = {"日经225": 40000, "韩国综合": 2600, "道琼斯": 43000,
            "纳斯达克": 19000, "标普500": 5800}
    out = []
    for name, code, _kw in _FOREIGN_MAP:
        pct = round(r.uniform(-2.2, 2.2), 2)
        out.append({
            "name": name, "code": code.split(".")[0],
            "price": round(base.get(name, 3000) * (1 + pct / 100), 2),
            "pct": pct,
        })
    return out


def _section_breadth(r: random.Random) -> Dict:
    up = r.randint(1200, 3200)
    down = r.randint(1200, 3200)
    return {
        "up": up, "down": down, "flat": r.randint(50, 300),
        "limit_up": r.randint(20, 120),
        "limit_down": r.randint(0, 40),
        "zhaban": r.randint(5, 60),      # 炸板家数
        "suspended": r.randint(0, 30),
    }


def _section_hot_boards(r: random.Random) -> List[Dict]:
    picked = r.sample(_BOARD_POOL, k=6)
    out = []
    for name in picked:
        leader_name, leader_code = r.choice(_STOCK_POOL)
        out.append({
            "name": name,
            "pct": round(r.uniform(-3, 8), 2),
            "leader": leader_name, "leader_code": leader_code,
            "leader_pct": round(r.uniform(-2, 12), 2),
            "net_in": round(r.uniform(-8, 20) * 1e8, 2),      # 主力净流入 (元)
        })
    out.sort(key=lambda x: x["pct"], reverse=True)
    return out


def _section_stock_list(r: random.Random, kind: str, n: int = 10) -> List[Dict]:
    """涨停/跌停/炸板/异动个股占位快照 (按涨跌幅降序)"""
    pool = _STOCK_POOL[:]
    r.shuffle(pool)
    out = []
    for name, code in pool[:n]:
        if kind == "limit_up":
            pct = round(r.uniform(9.8, 10.05), 2)
        elif kind == "limit_down":
            pct = round(r.uniform(-10.05, -9.8), 2)
        elif kind == "zhaban":
            pct = round(r.uniform(-3, 6), 2)
        else:
            pct = round(r.uniform(3, 11), 2)
        out.append({
            "name": name, "code": code,
            "price": round(r.uniform(5, 300), 2),
            "pct": pct,
            "turnover": round(r.uniform(0.5, 35), 2),         # 换手率 %
        })
    out.sort(key=lambda x: x["pct"], reverse=True)
    return out


def mock_panorama(seed: Optional[int] = None) -> Dict:
    """看盘全景快照 (功能储备: 占位数据, 不实际抓取)"""
    r = _rng(seed)
    return {
        "mode": "reserve",                       # 功能储备: 前端据此展示占位提示
        "sources": list(SOURCES),
        "sections": list(SECTIONS),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "indices": _section_indices(r),
        "foreign": _section_foreign(r),
        "breadth": _section_breadth(r),
        "hot_boards": _section_hot_boards(r),
        "hot_stocks": _section_stock_list(r, "hot", 12),
        "limit_up_today": _section_stock_list(r, "limit_up"),
        "limit_up_prev": _section_stock_list(r, "limit_up"),
        "limit_down": _section_stock_list(r, "limit_down"),
        "zhaban": _section_stock_list(r, "zhaban"),
        "live_sections": [],
        "errors": [],
    }


# ---------------------------------------------------------------------------
# 同花顺 iFinD 真实数据接入
# ---------------------------------------------------------------------------

class _Unavailable(RuntimeError):
    """某板块真实数据不可用 (取数失败/字段缺失), 触发该板块占位回落"""


# iFinD 指数主体 (同花顺标准代码, 单次请求上限 10 个)
_IFIND_INDEXES = [
    ("上证指数", "000001.SH"), ("深证成指", "399001.SZ"), ("创业板指", "399006.SZ"),
    ("科创50", "000688.SH"), ("沪深300", "000300.SH"), ("北证50", "899050.BJ"),
]


def _num(v: Any) -> Optional[float]:
    """尽力将 iFinD 返回值转为 float (兼容 '12.3%' / '1,234' 等文本), 失败返回 None"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip().replace(",", "").replace("%", ""))
        except ValueError:
            return None
    return None


def _text(row: Dict, *names: str) -> Optional[str]:
    """按候选字段名取字符串 (兼容 iFinD 中英文/带后缀的字段命名)"""
    for n in names:
        v = row.get(n)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if v is not None and not isinstance(v, (dict, list)):
            return str(v)
    return None


def _pick(row: Dict, *names: str) -> Optional[float]:
    """按候选字段名取数值"""
    for n in names:
        if n in row:
            v = _num(row[n])
            if v is not None:
                return v
    return None


def _norm_key(h: Any) -> str:
    """归一化 iFinD 表头 -> 基础指标名 (去掉 ':前复权' / '[日期]' / '（单位：元）' 等后缀)"""
    s = str(h)
    for ch in ("：", ":"):
        if ch in s:
            s = s.split(ch)[0]
    for ch in ("（", "(", "["):
        if ch in s:
            s = s.split(ch)[0]
    return s.strip()


def _table_rows(table: Any) -> List[Dict]:
    """iFinD ``data.tables`` (首个子列表为表头) -> 行字典列表; 同名列保留首次出现"""
    if not isinstance(table, list) or not table or not isinstance(table[0], list):
        return []
    header = [_norm_key(c) for c in table[0]]
    out = []
    for raw in table[1:]:
        if not isinstance(raw, list):
            continue
        row: Dict[str, Any] = {}
        for i, key in enumerate(header):
            if key and key not in row:            # 跳过重复表头 (如 涨跌幅/涨跌幅排名)
                row[key] = raw[i] if i < len(raw) else None
        if row:
            out.append(row)
    return out


def _md_rows(text: str) -> List[Dict]:
    """解析 iFinD 自然语言工具返回的 Markdown 表格 (``data.answer``)

    兼容一次 answer 内含**多张表** (多指标查询会分表返回): 分隔行 ``|---|---|``
    的上一行即当前表的表头, 据此逐表重建表头, 避免第二张表的指标名被按第一张表的
    表头错位解析。
    """
    rows: List[Dict] = []
    header: Optional[List[str]] = None
    pending: Optional[List[str]] = None      # 待定行: 下一行是分隔行则它是表头, 否则是数据行

    def _flush(cells: List[str]) -> None:
        if header is None:
            return
        padded = cells + [""] * (len(header) - len(cells)) if len(cells) < len(header) else cells
        row: Dict[str, Any] = {}
        for i, key in enumerate(header):
            if key and key not in row:
                row[key] = padded[i]
        if row:
            rows.append(row)

    for line in str(text).splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if all(set(c) <= set("-: ") for c in cells):       # 分隔行 |---|---|
            if pending is not None:                        # 上一行是表头
                header = [_norm_key(c) for c in pending]
                pending = None
            continue
        if pending is not None:                            # 上一行是数据行
            _flush(pending)
        pending = cells
    if pending is not None:
        _flush(pending)
    return rows


def _rows_of(data: Any) -> List[Dict]:
    """将 iFinD 多种返回结构归一化成行列表 [{...}]"""
    if isinstance(data, str):
        return _md_rows(data)
    if isinstance(data, dict):
        ans = data.get("answer")
        if isinstance(ans, str) and "|" in ans:          # 自然语言工具: Markdown 表格
            got = _md_rows(ans)
            if got:
                return got
        tables = data.get("tables")
        if isinstance(tables, list) and tables and isinstance(tables[0], list):
            got = _table_rows(tables)                    # 高频行情工具: tables(表头+数据)
            if got:
                return got
        for key in ("data", "result", "rows", "items", "list", "records"):
            if isinstance(data.get(key), (dict, list)):
                got = _rows_of(data[key])
                if got:
                    return got
        inner = [v for v in data.values() if isinstance(v, dict)]
        if inner and len(inner) == len(data):        # {主体: {指标: 值}}
            out = []
            for k, v in data.items():
                row = dict(v)
                row.setdefault("name", k)
                row.setdefault("symbol", k)
                out.append(row)
            return out
        return [data]
    if isinstance(data, list):
        return [it for it in data if isinstance(it, dict)]
    return []


def _amount(v: Any) -> Optional[float]:
    """iFinD 金额文本 -> 元 ('23.34亿' / '2.5万' / '2.97E9' / 裸数字)"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "")
    if not s or s.lower() == "null":
        return None
    try:
        if s.endswith("亿"):
            return float(s[:-1]) * 1e8
        if s.endswith("万"):
            return float(s[:-1]) * 1e4
        return float(s)
    except ValueError:
        return None


def _match_symbol(rows: List[Dict], code: str, name: str) -> Optional[Dict]:
    """在行列表中按代码/名称定位主体所在行"""
    code6 = code.split(".")[0]
    kw = name.rstrip("指数")
    for row in rows:
        blob = " ".join(str(x) for x in row.keys()) + " " + " ".join(str(x) for x in row.values())
        if code in blob or code6 in blob or name in blob or (kw and kw in blob):
            return row
    return None


# 会话内节流 (iFinD 个人版有调用频率限制, 429 时回落占位)
_rate_lock = threading.Lock()
_last_call = {"t": 0.0}


def _pace(gap: float = 1.0) -> None:
    """两次 iFinD 调用之间至少间隔 ``gap`` 秒, 避免触发网关限流"""
    with _rate_lock:
        wait = gap - (time.time() - _last_call["t"])
        if wait > 0:
            time.sleep(wait)
        _last_call["t"] = time.time()


def _guard(ok: bool, data: Any) -> Any:
    """调用失败 / 网关限流 / 空结果 -> 抛 _Unavailable 触发该板块占位回落"""
    if not ok:
        raise _Unavailable(str(data))
    if isinstance(data, str) and ("call failed" in data or "过于频繁" in data):
        raise _Unavailable(data[:80])
    return data


# 指数快照缓存 (indices 与 breadth 共用一次请求)
_snap_lock = threading.Lock()
_SNAP: Dict[str, Any] = {"ts": 0.0, "rows": []}
_SNAP_TTL = 30.0


def _index_snapshot() -> List[Dict]:
    """指数快照 (最新价/涨跌幅/成交额 + 上涨/下跌/涨跌停/停牌家数), 30s 内复用"""
    with _snap_lock:
        if _SNAP["rows"] and time.time() - _SNAP["ts"] < _SNAP_TTL:
            return _SNAP["rows"]
        _pace()
        ok, data = ifind_client.call_ifind("index", "index_highfreq_quotes", {
            "symbols": ",".join(c for _, c in _IFIND_INDEXES),
            "indicators": "最新价,涨跌幅,成交额,上涨家数,下跌家数,涨停家数,跌停家数,停牌家数",
            "data_mode": "real_time",
        })
        _guard(ok, data)
        rows = _rows_of(data)
        if not rows:
            raise _Unavailable("指数快照返回为空")
        _SNAP.update(ts=time.time(), rows=rows)
        return rows


def _live_indices() -> List[Dict]:
    """指数快照: index_highfreq_quotes (real_time)"""
    rows = _index_snapshot()
    out = []
    for name, code in _IFIND_INDEXES:
        row = _match_symbol(rows, code, name)
        if row is None:
            raise _Unavailable(f"{name} 未返回数据")
        price = _pick(row, "最新价", "最新成交价", "收盘价", "price")
        if price is None:
            raise _Unavailable(f"{name} 缺少最新价")
        out.append({
            "name": name, "code": code.split(".")[0],
            "price": round(price, 2),
            "pct": round(_pick(row, "涨跌幅", "pct") or 0.0, 2),
            "amount": _amount(_pick(row, "成交额", "amount")) or 0.0,
        })
    return out


# 外盘指数缓存 (自然语言查询 index_data)
_FOREIGN_CACHE: Dict[str, Any] = {"ts": 0.0, "data": None}
_FOREIGN_TTL = 120.0


def _live_foreign() -> List[Dict]:
    """日韩美股指数: index_data 自然语言查询 (高频接口不支持外盘主体代码)"""
    if _FOREIGN_CACHE["data"] and time.time() - _FOREIGN_CACHE["ts"] < _FOREIGN_TTL:
        return _FOREIGN_CACHE["data"]
    _pace()
    ok, data = ifind_client.call_ifind("index", "index_data", {"query": _FOREIGN_QUERY})
    _guard(ok, data)
    rows = _rows_of(data)
    if not rows:
        raise _Unavailable("外盘指数返回为空")
    out = []
    for name, code, kw in _FOREIGN_MAP:
        row = _match_symbol(rows, code, kw)
        if row is None:
            continue
        price = _pick(row, "收盘价", "最新价", "点位", "price")
        if price is None:
            continue
        out.append({
            "name": name, "code": code.split(".")[0],
            "price": round(price, 2),
            "pct": round(_pick(row, "涨跌幅", "pct") or 0.0, 2),
        })
    if not out:
        raise _Unavailable("外盘指数解析为空")
    _FOREIGN_CACHE.update(ts=time.time(), data=out)
    return out


def _live_breadth() -> Dict:
    """涨跌家数: 沪深两市指数宽度相加 (沪 000001.SH + 深 399001.SZ)

    注: iFinD 无「平盘家数」「炸板家数」口径, 该两项为 null (前端展示占位)。
    """
    rows = _index_snapshot()

    def _row(code6: str) -> Optional[Dict]:
        for r in rows:
            if str(r.get("证券代码", "")).split(".")[0] == code6:
                return r
        return None

    sh, sz = _row("000001"), _row("399001")
    if sh is None and sz is None:
        raise _Unavailable("涨跌家数未返回")

    def _sum(*names: str) -> int:
        return int(sum(_pick(r, *names) or 0 for r in (sh, sz) if r is not None))

    if _pick(sh or {}, "上涨家数") is None and _pick(sz or {}, "上涨家数") is None:
        raise _Unavailable("涨跌家数未返回")
    return {
        "up": _sum("上涨家数", "up"), "down": _sum("下跌家数", "down"), "flat": None,
        "limit_up": _sum("涨停家数"),
        "limit_down": _sum("跌停家数"),
        "zhaban": None,
        "suspended": _sum("停牌家数"),
    }


# 板块快照缓存: hot_boards 列表 + 板块净流入映射 (sector_data 一次取回)
_board_lock = threading.Lock()
_BOARD_CACHE: Dict[str, Any] = {"ts": 0.0, "data": None}
_BOARD_TTL = 60.0
_BOARD_N = 8            # 按行业涨跌幅取前 N 个板块做净流入取数 (热榜展示前 6)
_HOT_N = 6


def _board_net_map(names: List[str]) -> Dict[str, float]:
    """按板块名取「净主动买入额(合计)」(元); 取数失败返回空映射 (仅影响净额展示)

    注: iFinD 口径仅对「行业」板块名稳定命中 (概念名会匹配到港股/美股同名板块),
    故调用方传入的必须是新浪「行业」板块名。
    """
    if not names:
        return {}
    _pace()
    ok, data = ifind_client.call_ifind("index", "sector_data", {
        "query": "同花顺行业板块 " + "、".join(names) + " 的净主动买入额(合计)",
    })
    if not ok:
        return {}
    out: Dict[str, float] = {}
    for row in _rows_of(data):
        full = _text(row, "证券简称", "名称", "name") or ""
        board = full.split("->")[-1].split("|")[-1].strip()
        val = _amount(_text(row, "净主动买入额", "净主动买入额(合计)"))
        if board and val is not None:
            out.setdefault(board, val)          # 同名板块保留首次出现
    return out


def _live_board_pool(n: int = _BOARD_N) -> List[Dict]:
    """新浪「行业」板块快照, 按涨跌幅降序取前 n"""
    from . import boards as boards_svc
    rows = [b for b in boards_svc.list_boards("industry")
            if b.get("name") and b.get("pct") is not None]
    rows.sort(key=lambda b: float(b["pct"]), reverse=True)
    return rows[:n]


def _board_bundle() -> List[Dict]:
    """板块异动: 新浪行业板块实时涨跌幅/领涨股 + iFinD 净主动买入额 (60s 缓存)"""
    with _board_lock:
        if _BOARD_CACHE["data"] and time.time() - _BOARD_CACHE["ts"] < _BOARD_TTL:
            return _BOARD_CACHE["data"]
        pool = _live_board_pool()
        if not pool:
            raise _Unavailable("行业板块快照为空 (新浪板块列表不可用)")
        netmap = _board_net_map([b["name"] for b in pool])
        hot = [{
            "name": b["name"], "pct": round(float(b["pct"]), 2),
            "leader": b.get("leader") or "--",
            "leader_code": "",                    # 新浪板块快照不含领涨股代码
            "leader_pct": round(float(b.get("leader_pct") or 0.0), 2),
            "net_in": netmap.get(b["name"]),
        } for b in pool[:_HOT_N]]
        _BOARD_CACHE.update(ts=time.time(), data=hot)
        return hot


def _live_hot_boards() -> List[Dict]:
    """板块异动: 新浪行业板块快照 + iFinD 净主动买入额"""
    return _board_bundle()


# 智能选股查询 (异动个股 / 涨停 / 昨日涨停 / 跌停 / 炸板)
_GAIN_QUERY = "今日涨幅前20的A股股票，含最新价、涨跌幅、换手率、成交额"
_LIMIT_UP_TODAY_QUERY = "今日涨停的A股股票，含最新价、涨跌幅、换手率"
_LIMIT_UP_PREV_QUERY = "昨日涨停的A股股票，含最新价、涨跌幅、换手率"
_LIMIT_DOWN_QUERY = "今日跌停的A股股票，含最新价、涨跌幅、换手率"
_ZHABAN_QUERY = "今日炸板的A股股票，含最新价、涨跌幅、换手率"

_reg_singleton: Any = None


def _registry():
    """本地股票注册表单例 (用于过滤掉点击后无法展示 K 线的标的)"""
    global _reg_singleton
    if _reg_singleton is None:
        from ..data.stock_registry import StockRegistry
        _reg_singleton = StockRegistry()
    return _reg_singleton


# 个股列表缓存 (search_stocks 逐条查询较慢, 60s 内复用)
_list_lock = threading.Lock()
_LIST_CACHE: Dict[str, Any] = {}
_LIST_TTL = 60.0


def _stock_rows(query: str, limit: int = 10) -> List[Dict]:
    """智能选股查询 -> 归一化个股行 (按涨跌幅降序, 过滤本地无 K 线标的)"""
    with _list_lock:
        hit = _LIST_CACHE.get(query)
        if hit and time.time() - hit[0] < _LIST_TTL:
            return hit[1]
    _pace()
    ok, data = ifind_client.call_ifind("stock", "search_stocks", {"query": query})
    _guard(ok, data)
    rows: List[Dict] = []
    for row in _rows_of(data):
        name = _text(row, "股票简称", "股票名称", "名称", "name")
        code = (_text(row, "股票代码", "代码", "code") or "").split(".")[0]
        pct = _pick(row, "涨跌幅", "涨幅", "pct")
        if not name or not code or pct is None:
            continue
        rows.append({
            "name": name, "code": code,
            "price": round(_pick(row, "收盘价", "最新价", "现价", "price") or 0.0, 2),
            "pct": round(pct, 2),
            "turnover": round(_pick(row, "换手率", "turnover") or 0.0, 2),
        })
    if not rows:
        raise _Unavailable("智能选股返回为空")
    seen = set()
    uniq = [r for r in rows if r["code"] not in seen and not seen.add(r["code"])]
    reg = _registry()
    # 新股/北交所标的本地无 K 线数据, 点击看盘跳转会失败 —— 直接过滤, 保证榜上每只都可查看
    uniq = [r for r in uniq if reg.get(r["code"])]
    uniq.sort(key=lambda x: x["pct"], reverse=True)
    out = uniq[:limit]
    if not out:
        raise _Unavailable("智能选股结果均为本地无 K 线标的")
    with _list_lock:
        _LIST_CACHE[query] = (time.time(), out)
    return out


def _live_hot_stocks() -> List[Dict]:
    """异动个股: 涨幅前列 (按涨跌幅降序)"""
    return _stock_rows(_GAIN_QUERY, 12)


def _live_limit_up_today() -> List[Dict]:
    """当日涨停股"""
    return _stock_rows(_LIMIT_UP_TODAY_QUERY, 12)


def _live_limit_up_prev() -> List[Dict]:
    """昨日涨停股"""
    return _stock_rows(_LIMIT_UP_PREV_QUERY, 12)


def _live_limit_down() -> List[Dict]:
    """跌停股"""
    return _stock_rows(_LIMIT_DOWN_QUERY, 12)


def _live_zhaban() -> List[Dict]:
    """炸板股 (曾涨停)"""
    return _stock_rows(_ZHABAN_QUERY, 12)


# 需求101602: 全景整体结果缓存 —— 冷启动实测约 6~10s, 属「响应过长」;
# 各子段的 TTL(30~120s) 到期会触发整段重算, 故再叠一层整体结果缓存 + 单飞,
# 使窗口内的重复请求(含预加载预热结果)直接命中缓存 (<0.5s)。
_PANORAMA_TTL = 60.0
_PANORAMA_CACHE: Dict[str, Any] = {"ts": 0.0, "data": None}
_PANORAMA_LOCK = threading.Lock()
# 需求101607: 后台刷新在跑标志 (幂等, 避免并发的过期请求各起一个重算线程)
_PANORAMA_BUSY = False


def _refresh_panorama_bg() -> None:
    """需求101607: 后台重算整体快照 (stale-while-revalidate)

    缓存过期但仍有旧值时, 由 real_panorama 立即返回旧值并调用本函数; 重算(6~10s)在
    后台线程完成, 用户请求不再被这段长尾阻塞。幂等: 已有刷新在跑则直接返回。
    """
    global _PANORAMA_BUSY
    with _PANORAMA_LOCK:
        if _PANORAMA_BUSY:
            return
        _PANORAMA_BUSY = True

    def _run():
        global _PANORAMA_BUSY
        try:
            data = _real_panorama_calc()
            _PANORAMA_CACHE["ts"] = time.time()
            _PANORAMA_CACHE["data"] = data
        except Exception as e:                 # noqa: BLE001
            print(f"[Watch] 全景后台刷新失败: {e}")
        finally:
            with _PANORAMA_LOCK:
                _PANORAMA_BUSY = False

    threading.Thread(target=_run, name="panorama-refresh", daemon=True).start()


def real_panorama(seed: Optional[int] = None) -> Dict:
    """真实看盘全景快照 (整体缓存 + 单飞 + stale-while-revalidate; 需求101602/101607)"""
    if seed is not None:                   # 指定 seed 走占位快照, 不缓存
        return _real_panorama_calc(seed)
    now = time.time()
    hit = _PANORAMA_CACHE["data"]
    if hit is not None:
        if now - _PANORAMA_CACHE["ts"] < _PANORAMA_TTL:
            return hit
        _refresh_panorama_bg()             # 需求101607: 立即返回旧值 + 后台刷新, 消除长尾
        return hit
    with _PANORAMA_LOCK:                   # 进程首次 (通常已被预加载预热): 同步单飞计算
        hit = _PANORAMA_CACHE["data"]
        if hit is not None and time.time() - _PANORAMA_CACHE["ts"] < _PANORAMA_TTL:
            return hit
        data = _real_panorama_calc(seed)
        _PANORAMA_CACHE["ts"] = time.time()
        _PANORAMA_CACHE["data"] = data
        return data


def _real_panorama_calc(seed: Optional[int] = None) -> Dict:
    """真实看盘全景快照计算体 (同花顺 iFinD, 逐板块容错)

    任一段失败仅回落该段占位数据, 其余板块不受影响; ``mode='live'`` 表示至少
    一段为真实数据, ``'reserve'`` 表示全部回落 (密钥缺失/认证失败/网络异常)。
    """
    r = _rng(seed)
    live: List[str] = []
    errors: List[str] = []
    auth_blocked = False

    def _section(key: str, fetch, fallback):
        nonlocal auth_blocked
        if auth_blocked:                       # 认证失败后不再重复打网关
            errors.append(f"{key}: 跳过 (密钥不可用)")
            return fallback
        try:
            val = fetch()
            live.append(key)
            return val
        except Exception as e:                 # noqa: BLE001 - 逐段容错, 保证接口可用
            errors.append(f"{key}: {e}")
            if "认证失败" in str(e) or "密钥" in str(e):
                auth_blocked = True
            return fallback

    # 先完成各段取数, 再组装返回体 —— 否则 dict 字面量里的 mode 会在 section 求值前就
    # 按空的 live 列表算成 "reserve" (值为 list 引用却被后续 append 刷新的 live_sections 打脸)。
    sections = {
        "indices": _section("indices", _live_indices, _section_indices(r)),
        "foreign": _section("foreign", _live_foreign, _section_foreign(r)),
        "breadth": _section("breadth", _live_breadth, _section_breadth(r)),
        "hot_boards": _section("hot_boards", _live_hot_boards, _section_hot_boards(r)),
        "hot_stocks": _section("hot_stocks", _live_hot_stocks, _section_stock_list(r, "hot", 12)),
        "limit_up_today": _section("limit_up_today", _live_limit_up_today, _section_stock_list(r, "limit_up")),
        "limit_up_prev": _section("limit_up_prev", _live_limit_up_prev, _section_stock_list(r, "limit_up")),
        "limit_down": _section("limit_down", _live_limit_down, _section_stock_list(r, "limit_down")),
        "zhaban": _section("zhaban", _live_zhaban, _section_stock_list(r, "zhaban")),
    }
    return {
        "mode": "live" if live else "reserve",
        "sources": [LIVE_SOURCE, "新浪财经"] if live else list(SOURCES),
        "sections": list(SECTIONS),
        "live_sections": live,
        "errors": errors,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        **sections,
    }
