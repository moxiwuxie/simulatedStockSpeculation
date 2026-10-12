"""动态分组服务 —— 规则驱动的实时选股分组

与「自选分组」(watchlist: 用户手动加股, 持久化个股) 相对:
  动态分组只持久化「规则」(指标 + 操作符 + 阈值), 每次查询对全市场快照
  重新求值, 动态返回命中个股。

规则结构 (多条件 AND):
  {"logic": "AND", "conditions": [{"metric": "is_limit_up", "op": "eq", "value": true}, ...]}
"""
from __future__ import annotations
import json
import sqlite3
import threading
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from .market_spot import market_spot


# ---------- 指标注册表 (前端据此渲染条件编辑器; 后续可扩充至同花顺全量指标) ----------
NUM_OPS = ["gt", "gte", "lt", "lte", "between"]
BOOL_OPS = ["eq"]

METRICS: List[dict] = [
    {"key": "is_limit_up",   "label": "涨停",     "type": "bool",   "unit": "",  "ops": BOOL_OPS},
    {"key": "is_limit_down", "label": "跌停",     "type": "bool",   "unit": "",  "ops": BOOL_OPS},
    {"key": "is_zhaban",     "label": "炸板(曾涨停)", "type": "bool", "unit": "", "ops": BOOL_OPS},
    {"key": "upper_shadow",  "label": "上影线比例", "type": "number", "unit": "%", "ops": NUM_OPS},
    {"key": "change_pct",    "label": "涨跌幅",   "type": "number", "unit": "%", "ops": NUM_OPS},
    {"key": "volume",        "label": "成交量",   "type": "number", "unit": "股", "ops": NUM_OPS},
    {"key": "amount",        "label": "成交额",   "type": "number", "unit": "元", "ops": NUM_OPS},
    {"key": "price",         "label": "最新价",   "type": "number", "unit": "元", "ops": NUM_OPS},
    {"key": "open",          "label": "开盘价",   "type": "number", "unit": "元", "ops": NUM_OPS},
    {"key": "close",         "label": "收盘价",   "type": "number", "unit": "元", "ops": NUM_OPS},
]
METRIC_MAP = {m["key"]: m for m in METRICS}
BOOL_METRICS = {m["key"] for m in METRICS if m["type"] == "bool"}
METRIC_KEYS = set(METRIC_MAP)

# close 与 price 在快照中同源 (最新价即收盘价口径)
_ALIAS = {"close": "price"}

# 内置只读预设分组 (不写库)
PRESETS: List[dict] = [
    {"id": "preset_limit_up", "name": "涨停股",
     "rules": {"logic": "AND", "conditions": [{"metric": "is_limit_up", "op": "eq", "value": True}]}},
    {"id": "preset_zhaban", "name": "炸板股",
     "rules": {"logic": "AND", "conditions": [{"metric": "is_zhaban", "op": "eq", "value": True}]}},
    {"id": "preset_limit_down", "name": "跌停股",
     "rules": {"logic": "AND", "conditions": [{"metric": "is_limit_down", "op": "eq", "value": True}]}},
]
PRESET_MAP = {p["id"]: p for p in PRESETS}

_OPS = {
    "gt": lambda a, b: a > b,
    "gte": lambda a, b: a >= b,
    "lt": lambda a, b: a < b,
    "lte": lambda a, b: a <= b,
    "eq": lambda a, b: a == b,
}


def _match_one(row: dict, cond: dict) -> bool:
    metric = cond.get("metric")
    op = cond.get("op") or "gt"
    val = cond.get("value")
    if metric not in METRIC_KEYS:
        return False
    actual = row.get(_ALIAS.get(metric, metric))
    if actual is None:
        return False
    try:
        if metric in BOOL_METRICS:
            if op != "eq":
                return False
            return bool(actual) == (val is True or str(val).lower() in ("true", "1"))
        a = float(actual)
        if op == "between":
            if not (isinstance(val, (list, tuple)) and len(val) == 2):
                return False
            lo, hi = float(val[0]), float(val[1])
            return lo <= a <= hi
        fn = _OPS.get(op)
        return bool(fn(a, float(val))) if fn else False
    except (TypeError, ValueError):
        return False


def evaluate(rules: Optional[dict], spot: Dict[str, dict],
             limit: int = 200, sort_by: str = "change_pct", desc: bool = True) -> List[dict]:
    """按规则 (多条件 AND) 对快照求值, 返回命中个股 (默认按涨跌幅倒序)"""
    conds = (rules or {}).get("conditions") or []
    if not conds:
        return []
    hits = [row for row in spot.values()
            if all(_match_one(row, c) for c in conds)]
    if sort_by:
        hits.sort(key=lambda r: r.get(sort_by) or 0, reverse=desc)
    return hits[:max(1, limit)]


def normalize_rules(rules: Optional[dict]) -> dict:
    """校验并归一化规则 (仅保留合法指标/操作符)"""
    conds_in = (rules or {}).get("conditions") or []
    conds: List[dict] = []
    for c in conds_in:
        key = (c or {}).get("metric")
        if key not in METRIC_KEYS:
            continue
        meta = METRIC_MAP[key]
        op = c.get("op") or ("eq" if meta["type"] == "bool" else "gt")
        if op not in meta["ops"]:
            op = "eq" if meta["type"] == "bool" else "gt"
        conds.append({"metric": key, "op": op, "value": c.get("value")})
    if not conds:
        raise ValueError("请至少配置一条有效的指标条件")
    return {"logic": "AND", "conditions": conds}


class DynGroupService:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
            return cls._instance

    def __init__(self):
        if hasattr(self, "_initialized"):
            return
        self._initialized = True
        self._db_path = Path(__file__).resolve().parent.parent / "data" / "dyn_groups.db"
        self._db_path.parent.mkdir(exist_ok=True)
        self._init_db()

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with closing(self._open()) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS dyn_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                name TEXT NOT NULL,
                rules TEXT NOT NULL,
                created_at TEXT,
                UNIQUE (user_id, name)
            )""")
            conn.commit()

    # ---------- CRUD ----------
    def list_groups(self, user_id: str) -> List[dict]:
        groups: List[dict] = [{"id": p["id"], "name": p["name"], "rules": p["rules"], "builtin": True}
                              for p in PRESETS]
        with closing(self._open()) as conn:
            rows = conn.execute(
                "SELECT id, name, rules FROM dyn_groups WHERE user_id=? ORDER BY id", (user_id,)).fetchall()
        for r in rows:
            try:
                rules = json.loads(r["rules"])
            except Exception:
                rules = {"logic": "AND", "conditions": []}
            groups.append({"id": r["id"], "name": r["name"], "rules": rules, "builtin": False})
        return groups

    def get_group(self, user_id: str, group_id) -> Optional[dict]:
        gid = str(group_id)
        if gid in PRESET_MAP:
            p = PRESET_MAP[gid]
            return {"id": p["id"], "name": p["name"], "rules": p["rules"], "builtin": True}
        with closing(self._open()) as conn:
            row = conn.execute("SELECT id, name, rules FROM dyn_groups WHERE id=? AND user_id=?",
                               (group_id, user_id)).fetchone()
        if not row:
            return None
        try:
            rules = json.loads(row["rules"])
        except Exception:
            rules = {"logic": "AND", "conditions": []}
        return {"id": row["id"], "name": row["name"], "rules": rules, "builtin": False}

    def add_group(self, user_id: str, name: str, rules: dict) -> dict:
        name = (name or "").strip()
        if not name:
            raise ValueError("动态分组名称不能为空")
        if name in {p["name"] for p in PRESETS}:
            raise ValueError(f"名称「{name}」与内置预设冲突")
        rules = normalize_rules(rules)
        try:
            with closing(self._open()) as conn:
                conn.execute("INSERT INTO dyn_groups (user_id, name, rules, created_at) VALUES (?,?,?,?)",
                             (user_id, name, json.dumps(rules, ensure_ascii=False),
                              datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                gid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                conn.commit()
        except sqlite3.IntegrityError:
            raise ValueError(f"动态分组「{name}」已存在")
        return {"id": gid, "name": name, "rules": rules, "builtin": False}

    def update_group(self, user_id: str, group_id, name: Optional[str] = None,
                     rules: Optional[dict] = None) -> dict:
        if str(group_id) in PRESET_MAP:
            raise ValueError("内置预设分组不可修改")
        cur = self.get_group(user_id, group_id)
        if not cur:
            raise ValueError("动态分组不存在")
        new_name = (name or cur["name"]).strip() or cur["name"]
        new_rules = normalize_rules(rules) if rules is not None else cur["rules"]
        try:
            with closing(self._open()) as conn:
                conn.execute("UPDATE dyn_groups SET name=?, rules=? WHERE id=? AND user_id=?",
                             (new_name, json.dumps(new_rules, ensure_ascii=False), group_id, user_id))
                conn.commit()
        except sqlite3.IntegrityError:
            raise ValueError(f"动态分组「{new_name}」已存在")
        return {"id": group_id, "name": new_name, "rules": new_rules, "builtin": False}

    def delete_group(self, user_id: str, group_id) -> bool:
        if str(group_id) in PRESET_MAP:
            raise ValueError("内置预设分组不可删除")
        with closing(self._open()) as conn:
            cur = conn.execute("DELETE FROM dyn_groups WHERE id=? AND user_id=?", (group_id, user_id))
            conn.commit()
            return cur.rowcount > 0

    # ---------- 求值 ----------
    def stocks(self, user_id: str, group_id, limit: int = 200,
               force: bool = False) -> Optional[List[dict]]:
        g = self.get_group(user_id, group_id)
        if not g:
            return None
        return evaluate(g["rules"], market_spot.get_spot(force=force), limit=limit)


dyn_group_svc = DynGroupService()
