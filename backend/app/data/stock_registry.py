"""全 A 股股票注册表 - 基于 akshare stock_info_a_code_name

职责:
  - 拉取全量 A 股列表 (code + name)
  - 按代码前缀推断市场/板块 (sh/sz/bj + kcb/gem)
  - SQLite 持久化到 data/trading.db 的 stocks 表
  - 按关键字模糊搜索: 代码 / 名称 / 拼音首字母 / 全拼
"""
from __future__ import annotations
import sqlite3
import re
import time
from pathlib import Path
from typing import List, Dict, Optional


DB_PATH = Path(__file__).resolve().parent / "trading.db"

# 需求101602: /api/stocks 全量返回约 776KB, 属于「数据过大」; 加进程内缓存避免每次直查 SQLite
_LIST_TTL = 600      # 10 分钟


def pinyin_of(name: str) -> str:
    """名称 → '首字母|全拼' (如 贵州茅台 → 'gzmt|guizhoumaotai'), 供搜索模糊匹配"""
    if not name:
        return ""
    try:
        from pypinyin import lazy_pinyin, Style
        initials = "".join(lazy_pinyin(name, style=Style.FIRST_LETTER, errors=lambda x: [""]))
        full = "".join(lazy_pinyin(name, errors=lambda x: [""]))
        return f"{initials}|{full}".lower()
    except Exception:
        return ""


class StockRegistry:
    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or DB_PATH
        self._py_ready = False
        self._list_cache: Dict[tuple, tuple] = {}     # (market, board) → (ts, rows)
        self._ensure_table()

    # ========== DB ==========

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_table(self):
        with self._conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS stocks (
                code TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                market TEXT NOT NULL,   -- sh/sz/bj
                board TEXT,            -- main/kcb/gem (主板/科创板/创业板)
                is_kcb INTEGER DEFAULT 0,
                fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_stocks_name ON stocks(name)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_stocks_market ON stocks(market)")
            # 迁移: 拼音检索列 (幂等)
            cols = {r[1] for r in c.execute("PRAGMA table_info(stocks)").fetchall()}
            if "pinyin" not in cols:
                c.execute("ALTER TABLE stocks ADD COLUMN pinyin TEXT DEFAULT ''")
            c.execute("CREATE INDEX IF NOT EXISTS idx_stocks_pinyin ON stocks(pinyin)")

    # ========== 拉取 ==========

    def refresh(self) -> int:
        """从 akshare 拉取全量 A 股列表并 upsert，返回条数"""
        import akshare as ak
        import time
        print("[StockRegistry] 拉取全 A 股列表...")
        df = ak.stock_info_a_code_name()

        rows = []
        for _, r in df.iterrows():
            code = str(r["code"]).strip()
            name = str(r["name"]).strip()
            market, board, is_kcb = self._classify(code)
            rows.append((code, name, market, board, is_kcb, pinyin_of(name)))

        with self._conn() as c:
            c.executemany(
                "INSERT OR REPLACE INTO stocks (code, name, market, board, is_kcb, pinyin) VALUES (?,?,?,?,?,?)",
                rows
            )
        self._py_ready = True
        self._list_cache.clear()          # 数据变更后失效缓存
        print(f"[StockRegistry] ✅ 写入 {len(rows)} 只股票 (耗时 {time.time():.1f}s)")
        return len(rows)

    @staticmethod
    def _classify(code: str) -> tuple[str, str, int]:
        """代码 → 市场/板块/是否科创板"""
        if code.startswith(("6", "9")):
            market = "sh"
        elif code.startswith(("0", "3")):
            market = "sz"
        else:
            market = "bj"
        board = "main"
        is_kcb = 0
        if code.startswith("688"):
            board, is_kcb = "kcb", 1
        elif code.startswith("300"):
            board, is_kcb = "gem", 1
        return market, board, is_kcb

    # ========== 查询 ==========

    def get(self, code: str) -> Optional[Dict]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM stocks WHERE code=?", (code,)).fetchone()
            return dict(row) if row else None

    def search(self, keyword: str, limit: int = 30) -> List[Dict]:
        """模糊搜索: 代码 / 名称 / 拼音首字母 / 全拼; 代码与首字母前缀命中优先"""
        kw = (keyword or "").strip()
        if not kw:
            return []
        self._backfill_pinyin()
        like, pre = f"%{kw}%", f"{kw}%"
        with self._conn() as c:
            rows = c.execute(
                """SELECT * FROM stocks
                   WHERE code LIKE ? OR name LIKE ? OR pinyin LIKE ?
                   ORDER BY CASE
                       WHEN code = ? THEN 0
                       WHEN code LIKE ? THEN 1
                       WHEN pinyin LIKE ? THEN 2
                       WHEN name LIKE ? THEN 3
                       ELSE 4 END, code
                   LIMIT ?""",
                (like, like, like, kw, pre, pre, pre, limit)
            ).fetchall()
            return [dict(r) for r in rows]

    def _backfill_pinyin(self) -> int:
        """历史数据补齐拼音列 (首次调用时执行一次)"""
        if self._py_ready:
            return 0
        with self._conn() as c:
            rows = c.execute("SELECT code, name FROM stocks WHERE pinyin IS NULL OR pinyin=''").fetchall()
            if rows:
                c.executemany("UPDATE stocks SET pinyin=? WHERE code=?",
                              [(pinyin_of(r["name"]), r["code"]) for r in rows])
        self._py_ready = True
        return len(rows)

    def list_all(self, market: Optional[str] = None, board: Optional[str] = None) -> List[Dict]:
        key = (market, board)
        hit = self._list_cache.get(key)
        if hit and time.time() - hit[0] < _LIST_TTL:
            return hit[1]
        sql = "SELECT * FROM stocks WHERE 1=1"
        params: list = []
        if market:
            sql += " AND market=?"
            params.append(market)
        if board:
            sql += " AND board=?"
            params.append(board)
        with self._conn() as c:
            rows = c.execute(sql, params).fetchall()
        out = [dict(r) for r in rows]
        self._list_cache[key] = (time.time(), out)
        return out

    def count(self) -> int:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM stocks").fetchone()[0]

    def ensure_ready(self) -> int:
        """保证注册表非空，若空则拉取; 并补齐拼音检索列"""
        cnt = self.count()
        if cnt == 0:
            self.refresh()
        self._backfill_pinyin()
        return self.count()
