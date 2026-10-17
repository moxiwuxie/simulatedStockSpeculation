"""自选股分组服务 - 按用户隔离, SQLite 持久化

结构:
  groups       (id, user_id, name)        自选分组
  watch_items  (id, user_id, group_id, code, name, added_at)
每个用户至少一个「默认分组」, 未指定组的新增自选自动落入默认组。
"""
from __future__ import annotations
import sqlite3
import threading
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import List, Optional

_DEFAULT_GROUP = "自选"


class WatchlistService:
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
        self._db_path = Path(__file__).resolve().parent.parent / "data" / "watchlist.db"
        self._db_path.parent.mkdir(exist_ok=True)
        self._init_db()

    def _open(self) -> sqlite3.Connection | None:
        conn = sqlite3.connect(str(self._db_path), timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with closing(self._open()) as conn:
            c = conn.cursor()
            c.execute("""CREATE TABLE IF NOT EXISTS groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                name TEXT NOT NULL,
                UNIQUE (user_id, name)
            )""")
            c.execute("""CREATE TABLE IF NOT EXISTS watch_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                group_id INTEGER NOT NULL,
                code TEXT NOT NULL,
                name TEXT DEFAULT '',
                added_at TEXT,
                UNIQUE (group_id, code)
            )""")
            # 需求101506-2: watch_items 增 kind/meta, 支持板块与指数型自选 (老库平滑迁移)
            cols = {r["name"] for r in c.execute("PRAGMA table_info(watch_items)")}
            if "kind" not in cols:
                c.execute("ALTER TABLE watch_items ADD COLUMN kind TEXT DEFAULT 'stock'")
            if "meta" not in cols:
                c.execute("ALTER TABLE watch_items ADD COLUMN meta TEXT DEFAULT ''")
            conn.commit()

    # ---------- 分组 ----------
    def _default_group_id(self, conn: sqlite3.Connection, user_id: str) -> int:
        row = conn.execute(
            "SELECT id FROM groups WHERE user_id=? AND name=?", (user_id, _DEFAULT_GROUP)).fetchone()
        if row:
            return row["id"]
        # 新账号首次进入自选页时, 「分组列表」与「分组概览」会并发请求 → 两者同时建默认分组
        # 会撞 UNIQUE(user_id, name); 用 INSERT OR IGNORE 消解竞态, 再回查真实 id
        conn.execute("INSERT OR IGNORE INTO groups (user_id, name) VALUES (?,?)",
                     (user_id, _DEFAULT_GROUP))
        return conn.execute(
            "SELECT id FROM groups WHERE user_id=? AND name=?",
            (user_id, _DEFAULT_GROUP)).fetchone()["id"]

    def list_groups(self, user_id: str) -> List[dict]:
        with closing(self._open()) as conn:
            self._default_group_id(conn, user_id)
            conn.commit()
            groups = [{"id": r["id"], "name": r["name"], "items": []}
                      for r in conn.execute(
                          "SELECT id, name FROM groups WHERE user_id=? ORDER BY id", (user_id,))]
            for r in conn.execute(
                    "SELECT id, group_id, code, name, kind, meta FROM watch_items WHERE user_id=? ORDER BY id",
                    (user_id,)):
                g = next((x for x in groups if x["id"] == r["group_id"]), None)
                if g:
                    g["items"].append({
                        "id": r["id"], "code": r["code"], "name": r["name"],
                        "kind": r["kind"] or "stock", "meta": r["meta"] or "",
                    })
            return groups

    def add_group(self, user_id: str, name: str) -> dict:
        name = (name or "").strip()
        if not name:
            raise ValueError("分组名称不能为空")
        if name == _DEFAULT_GROUP:
            raise ValueError("默认分组已存在")
        try:
            with closing(self._open()) as conn:
                conn.execute("INSERT INTO groups (user_id, name) VALUES (?,?)", (user_id, name))
                gid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                conn.commit()
        except sqlite3.IntegrityError:
            raise ValueError(f"分组「{name}」已存在")
        return {"id": gid, "name": name, "items": []}

    def delete_group(self, user_id: str, group_id: int) -> bool:
        with closing(self._open()) as conn:
            row = conn.execute("SELECT name FROM groups WHERE id=? AND user_id=?",
                               (group_id, user_id)).fetchone()
            if not row:
                return False
            if row["name"] == _DEFAULT_GROUP:
                raise ValueError("默认分组不能删除")
            conn.execute("DELETE FROM watch_items WHERE group_id=? AND user_id=?", (group_id, user_id))
            conn.execute("DELETE FROM groups WHERE id=? AND user_id=?", (group_id, user_id))
            conn.commit()
            return True

    # ---------- 自选股 ----------
    def add_item(self, user_id: str, code: str, name: str = "",
                 group_id: Optional[int] = None, kind: str = "stock",
                 meta: str = "") -> dict:
        with closing(self._open()) as conn:
            if group_id is None:
                group_id = self._default_group_id(conn, user_id)
            else:
                exists = conn.execute(
                    "SELECT id FROM groups WHERE id=? AND user_id=?", (group_id, user_id)).fetchone()
                if not exists:
                    raise ValueError("分组不存在")
            try:
                conn.execute(
                    "INSERT INTO watch_items (user_id, group_id, code, name, added_at, kind, meta) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (user_id, group_id, code, name or code,
                     datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                     kind or "stock", meta or ""))
                conn.commit()
            except sqlite3.IntegrityError:
                return {"id": None, "code": code, "name": name or code,
                        "kind": kind or "stock", "exists": True}
            item_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            return {"id": item_id, "code": code, "name": name or code,
                    "kind": kind or "stock", "exists": False}

    def remove_item(self, user_id: str, item_id: int) -> bool:
        with closing(self._open()) as conn:
            conn.execute("DELETE FROM watch_items WHERE id=? AND user_id=?", (item_id, user_id))
            conn.commit()
            return True

    # ---------- 分组概览 (需求9: 顶部标签栏展示各分组涨跌幅) ----------
    # 需求101506-1/2: 报价提供器由 main.py 注入 (板块/指数需 ReplayFeed, 避免循环依赖)
    quote_provider = None

    def _q(self, item: dict) -> dict:
        """单个自选项报价 {price, change_pct, ...}; 未注入 provider 时回退全市场个股快照"""
        fn = getattr(self, "quote_provider", None)
        if fn:
            try:
                return fn(item) or {}
            except Exception:
                return {}
        from .market_spot import market_spot
        try:
            spot = market_spot.get_spot() or {}
        except Exception:
            spot = {}
        return spot.get(item.get("code")) or {}

    def group_items(self, user_id: str, group_id: Optional[int] = None) -> dict:
        """某分组下的自选项 (含 kind/meta, 不含报价)"""
        with closing(self._open()) as conn:
            if group_id is None:
                group_id = self._default_group_id(conn, user_id)
                conn.commit()
            rows = list(conn.execute(
                "SELECT id, code, name, kind, meta FROM watch_items "
                "WHERE user_id=? AND group_id=? ORDER BY id", (user_id, group_id)))
        return {
            "group_id": group_id,
            "items": [{"id": r["id"], "code": r["code"], "name": r["name"],
                       "kind": r["kind"] or "stock", "meta": r["meta"] or ""} for r in rows],
        }

    def group_quotes(self, user_id: str, group_id: Optional[int] = None) -> dict:
        """需求101506-1: 某分组自选项 + 报价一次性批量返回 (避免逐股串行请求)"""
        data = self.group_items(user_id, group_id)
        for it in data["items"]:
            q = self._q(it)
            it["price"] = q.get("price")
            it["prev_close"] = q.get("prev_close")
            it["change_pct"] = q.get("change_pct")
            it["suspended"] = q.get("suspended")
        return data

    def groups_overview(self, user_id: str) -> List[dict]:
        """各分组涨跌幅 = 成分项最新涨跌幅均值 (个股/板块/指数统一口径, 缺失则 pct=None)"""
        groups = self.list_groups(user_id)
        out = []
        for g in groups:
            pcts = []
            for it in g["items"]:
                v = self._q(it).get("change_pct")
                if v is not None:
                    try:
                        pcts.append(float(v))
                    except (TypeError, ValueError):
                        pass
            out.append({
                "id": g["id"], "name": g["name"], "count": len(g["items"]),
                "pct": round(sum(pcts) / len(pcts), 2) if pcts else None,
            })
        return out


watchlist_svc = WatchlistService()