"""系统后台服务 (需求101809) —— 访问IP收集 / 安全管理(网站基础防护功能储备)

数据落 ``data/admin.db`` (SQLite, 与 auth.db 分离, 避免与登录写操作争锁):
  visits           访问记录 (ip / method / path / ua / user_id / status / ts)
  ip_blocklist     封禁名单 (ip / reason / created_at / expires_at)
  security_settings 安全防护配置 (键值对)

安全防护为「基础储备」: 默认不改变现有行为 ——
  - 速率限制默认关闭 (rate_limit_enabled=0), 开启后按每 IP 每分钟阈值返回 429;
  - 封禁名单一旦有记录即生效 (命中 IP 的所有 /api 请求返回 403, 后台接口除外)。
"""
from __future__ import annotations
import sqlite3
import threading
import time
from collections import defaultdict, deque
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

# 安全防护默认配置 (字符串值, 便于前端表单统一处理)
DEFAULT_SETTINGS = {
    "rate_limit_enabled": "0",      # 1 = 启用每 IP 速率限制
    "rate_limit_per_min": "120",    # 每分钟允许请求数
    "ip_block_hours": "24",         # 手动封禁默认时长(小时), 0 = 永久
    "auto_block_max_per_day": "0",  # 单 IP 每日访问上限, 0 = 不限制 (储备)
    "record_visits": "1",           # 1 = 记录访问日志
}


class AdminService:
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
        self._db_path = Path(__file__).resolve().parent.parent / "data" / "admin.db"
        self._db_path.parent.mkdir(exist_ok=True)
        self._mem_lock = threading.Lock()
        self._windows: dict = defaultdict(deque)      # ip -> deque[请求时间戳]
        self._init_db()

    # ---------- 基础 ----------
    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with closing(self._conn()) as conn:
            c = conn.cursor()
            c.execute("""CREATE TABLE IF NOT EXISTS visits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ip TEXT,
                method TEXT,
                path TEXT,
                ua TEXT,
                user_id TEXT,
                status INTEGER,
                ts TEXT
            )""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_visits_ip ON visits(ip)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_visits_ts ON visits(ts)")
            c.execute("""CREATE TABLE IF NOT EXISTS ip_blocklist (
                ip TEXT PRIMARY KEY,
                reason TEXT,
                created_at TEXT,
                expires_at TEXT
            )""")
            c.execute("""CREATE TABLE IF NOT EXISTS security_settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT
            )""")
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for k, v in DEFAULT_SETTINGS.items():
                c.execute("INSERT OR IGNORE INTO security_settings (key, value, updated_at) VALUES (?,?,?)",
                          (k, v, now))
            conn.commit()
        self._purge_expired_blocks()

    # ---------- 访问记录 ----------
    def record_visit(self, ip: str, method: str, path: str, ua: str, user_id: str, status: int):
        if not self.get_security().get("record_visits", "1") == "1":
            return
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with closing(self._conn()) as conn:
            conn.execute(
                "INSERT INTO visits (ip, method, path, ua, user_id, status, ts) VALUES (?,?,?,?,?,?,?)",
                (ip or "", (method or "")[:8], (path or "")[:200], (ua or "")[:300],
                 user_id or "", int(status or 0), ts))
            conn.commit()

    def list_visits(self, limit: int = 50, offset: int = 0, ip: str = "") -> dict:
        where, params = "", []
        if (ip or "").strip():
            where = "WHERE ip LIKE ?"
            params = [f"%{ip.strip()}%"]
        with closing(self._conn()) as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM visits {where}", params).fetchone()[0]
            rows = conn.execute(
                f"SELECT id, ip, method, path, ua, user_id, status, ts FROM visits {where} "
                f"ORDER BY id DESC LIMIT ? OFFSET ?", params + [limit, offset]).fetchall()
        items = [{
            "id": r[0], "ip": r[1] or "", "method": r[2] or "", "path": r[3] or "",
            "ua": r[4] or "", "user_id": r[5] or "", "status": r[6], "ts": r[7] or "",
        } for r in rows]
        return {"total": total, "visits": items}

    def visit_stats(self, days: int = 14) -> dict:
        today = datetime.now().strftime("%Y-%m-%d")
        with closing(self._conn()) as conn:
            total = conn.execute("SELECT COUNT(*) FROM visits").fetchone()[0]
            today_cnt = conn.execute(
                "SELECT COUNT(*) FROM visits WHERE substr(ts, 1, 10) = ?", (today,)).fetchone()[0]
            uniq = conn.execute("SELECT COUNT(DISTINCT ip) FROM visits").fetchone()[0]
            uniq_today = conn.execute(
                "SELECT COUNT(DISTINCT ip) FROM visits WHERE substr(ts, 1, 10) = ?", (today,)).fetchone()[0]
            top_paths = conn.execute(
                "SELECT path, COUNT(*) c FROM visits GROUP BY path ORDER BY c DESC LIMIT 8").fetchall()
            top_ips = conn.execute(
                "SELECT ip, COUNT(*) c FROM visits GROUP BY ip ORDER BY c DESC LIMIT 8").fetchall()
            daily = conn.execute(
                "SELECT substr(ts, 1, 10) d, COUNT(*) c FROM visits GROUP BY d ORDER BY d DESC LIMIT ?",
                (max(1, min(int(days or 14), 60)),)).fetchall()
        return {
            "total": total, "today": today_cnt, "unique_ips": uniq, "unique_ips_today": uniq_today,
            "top_paths": [{"path": r[0], "count": r[1]} for r in top_paths],
            "top_ips": [{"ip": r[0], "count": r[1]} for r in top_ips],
            "daily": [{"date": r[0], "count": r[1]} for r in reversed(daily)],
        }

    def list_ips(self, limit: int = 50, offset: int = 0, order: str = "count") -> dict:
        order_col = "last_seen" if order == "recent" else "cnt"
        with closing(self._conn()) as conn:
            total = conn.execute("SELECT COUNT(DISTINCT ip) FROM visits").fetchone()[0]
            rows = conn.execute(
                f"SELECT ip, COUNT(*) cnt, MIN(ts) first_seen, MAX(ts) last_seen, "
                f"COUNT(DISTINCT user_id) users FROM visits GROUP BY ip "
                f"ORDER BY {order_col} DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
            blocks = {r[0]: r[1] for r in conn.execute(
                "SELECT ip, expires_at FROM ip_blocklist").fetchall()}
        items = []
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for r in rows:
            exp = blocks.get(r[0])
            blocked = r[0] in blocks and (not exp or exp > now)
            items.append({
                "ip": r[0], "count": r[1], "first_seen": r[2] or "", "last_seen": r[3] or "",
                "users": r[4], "blocked": bool(blocked),
                "block_expires": exp if blocked else "",
            })
        return {"total": total, "ips": items}

    def clear_visits(self, days: int = 0) -> int:
        """清理访问日志: days<=0 清空全部, 否则仅保留最近 N 天"""
        with closing(self._conn()) as conn:
            if days and days > 0:
                cut = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
                n = conn.execute("DELETE FROM visits WHERE ts < ?", (cut,)).rowcount
            else:
                n = conn.execute("DELETE FROM visits").rowcount
            conn.commit()
        return n

    # ---------- 封禁名单 ----------
    def _purge_expired_blocks(self):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with closing(self._conn()) as conn:
            conn.execute("DELETE FROM ip_blocklist WHERE expires_at IS NOT NULL AND expires_at != '' "
                         "AND expires_at < ?", (now,))
            conn.commit()

    def is_blocked(self, ip: str) -> bool:
        if not (ip or "").strip():
            return False
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with closing(self._conn()) as conn:
            row = conn.execute("SELECT expires_at FROM ip_blocklist WHERE ip = ?", (ip.strip(),)).fetchone()
        if not row:
            return False
        exp = row[0]
        if exp and exp < now:
            self.unblock_ip(ip)
            return False
        return True

    def block_ip(self, ip: str, reason: str = "", hours: int = 0) -> bool:
        ip = (ip or "").strip()
        if not ip:
            raise ValueError("IP 不能为空")
        hours = int(hours or 0)
        expires = ""
        if hours > 0:
            expires = (datetime.now() + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with closing(self._conn()) as conn:
            conn.execute(
                "INSERT INTO ip_blocklist (ip, reason, created_at, expires_at) VALUES (?,?,?,?) "
                "ON CONFLICT(ip) DO UPDATE SET reason = excluded.reason, "
                "created_at = excluded.created_at, expires_at = excluded.expires_at",
                (ip, (reason or "")[:200], now, expires))
            conn.commit()
        return True

    def unblock_ip(self, ip: str) -> bool:
        with closing(self._conn()) as conn:
            n = conn.execute("DELETE FROM ip_blocklist WHERE ip = ?", ((ip or "").strip(),)).rowcount
            conn.commit()
        return n > 0

    def list_blocks(self) -> dict:
        self._purge_expired_blocks()
        with closing(self._conn()) as conn:
            rows = conn.execute(
                "SELECT ip, reason, created_at, expires_at FROM ip_blocklist ORDER BY created_at DESC").fetchall()
        return {"blocks": [{"ip": r[0], "reason": r[1] or "", "created_at": r[2] or "",
                            "expires_at": r[3] or ""} for r in rows]}

    # ---------- 安全防护配置 ----------
    def get_security(self) -> dict:
        with closing(self._conn()) as conn:
            rows = conn.execute("SELECT key, value FROM security_settings").fetchall()
        data = {r[0]: (r[1] or "") for r in rows}
        for k, v in DEFAULT_SETTINGS.items():
            data.setdefault(k, v)
        return data

    def set_security(self, settings: dict) -> dict:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with closing(self._conn()) as conn:
            for k, v in (settings or {}).items():
                if k not in DEFAULT_SETTINGS:
                    continue
                conn.execute(
                    "INSERT INTO security_settings (key, value, updated_at) VALUES (?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                    (k, str(v), now))
            conn.commit()
        return self.get_security()

    # ---------- 速率限制 (内存滑动窗口) ----------
    def check_rate(self, ip: str) -> bool:
        """返回 True 表示放行; False 表示触发速率限制 (429)"""
        sec = self.get_security()
        if sec.get("rate_limit_enabled") != "1":
            return True
        try:
            limit = int(sec.get("rate_limit_per_min") or 120)
        except ValueError:
            limit = 120
        if limit <= 0:
            return True
        now = time.time()
        with self._mem_lock:
            q = self._windows[ip or "-"]
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            if len(self._windows) > 8000:           # 防内存膨胀: 清理空窗口
                for k in [k for k, dq in self._windows.items() if not dq]:
                    self._windows.pop(k, None)
        return True


admin_svc = AdminService()
