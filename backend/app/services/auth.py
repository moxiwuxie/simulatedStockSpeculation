"""认证服务 - 用户注册 / 登录 / 注销 + 令牌会话管理

密码安全存储: PBKDF2-HMAC-SHA256 (随机盐, 100k 迭代), 明文不落库
令牌: secrets.token_urlsafe, 会话存 auth.db, 默认 7 天有效期
账号资料: 手机号(必填, 全局唯一) + 邮箱(选填)
"""
from __future__ import annotations
import hashlib
import re
import secrets
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

_SALT_BYTES = 16
_ITERATIONS = 100_000
_TOKEN_DAYS = 7

_PHONE_RE = re.compile(r"^1[3-9]\d{9}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# 账号格式约定: 4-20 位, 字母开头, 仅含字母/数字/下划线 (便于账号唯一且可读)
_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,19}$")
# 密码复杂度: 长度 8-64, 且需包含 [大写/小写/数字/特殊字符] 中至少三类
_PW_CLASS_RES = (
    re.compile(r"[a-z]"),
    re.compile(r"[A-Z]"),
    re.compile(r"\d"),
    re.compile(r"[^A-Za-z0-9]"),
)
_PW_MIN_LEN = 8
_PW_MAX_LEN = 64
_PW_MIN_CLASSES = 3


class AuthService:
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
        self._db_path = Path(__file__).resolve().parent.parent / "data" / "auth.db"
        self._db_path.parent.mkdir(exist_ok=True)
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with closing(self._conn()) as conn:
            c = conn.cursor()
            c.execute("""CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                created_at TEXT
            )""")
            c.execute("""CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )""")
            # 迁移: 旧库补 phone / email 列
            cols = {r[1] for r in c.execute("PRAGMA table_info(users)").fetchall()}
            if "phone" not in cols:
                c.execute("ALTER TABLE users ADD COLUMN phone TEXT")
            if "email" not in cols:
                c.execute("ALTER TABLE users ADD COLUMN email TEXT")
            # 需求101809: 系统后台账户管理 —— 角色 / 状态 / 最近登录 (幂等补列)
            if "role" not in cols:
                c.execute("ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'user'")
            if "status" not in cols:
                c.execute("ALTER TABLE users ADD COLUMN status TEXT DEFAULT 'active'")
            if "last_login_at" not in cols:
                c.execute("ALTER TABLE users ADD COLUMN last_login_at TEXT")
            # 手机号全局唯一 (允许多行为 NULL)
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_phone ON users(phone)")
            conn.commit()
            self._seed_admin(conn)

    def _seed_admin(self, conn: sqlite3.Connection):
        """需求101809: 确保系统存在一个管理员账号 (无则按环境变量/默认值创建)

        默认账号 admin / Admin@123456, 可通过环境变量 ADMIN_USERNAME / ADMIN_PASSWORD
        覆盖; 若同名账号已存在则直接提升为管理员, 不重置其密码。
        """
        row = conn.execute("SELECT 1 FROM users WHERE role = 'admin' LIMIT 1").fetchone()
        if row:
            return
        import os
        username = (os.getenv("ADMIN_USERNAME") or "admin").strip()
        password = os.getenv("ADMIN_PASSWORD") or "Admin@123456"
        phone = (os.getenv("ADMIN_PHONE") or "13900000000").strip()
        exist = conn.execute("SELECT user_id FROM users WHERE username = ?", (username,)).fetchone()
        if exist:
            conn.execute("UPDATE users SET role = 'admin' WHERE user_id = ?", (exist[0],))
            conn.commit()
            return
        salt, pw_hash = self._hash_password(password)
        user_id = "u_" + secrets.token_urlsafe(8)
        conn.execute(
            "INSERT INTO users (user_id, username, password_hash, salt, phone, email, created_at, role, status) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (user_id, username, pw_hash, salt, phone or None, None,
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "admin", "active"),
        )
        conn.commit()
        print(f"[Auth] 已创建管理员账号: {username} (密码可用 ADMIN_PASSWORD 环境变量覆盖)")

    # ---------- 校验 ----------
    @staticmethod
    def valid_phone(phone: str) -> bool:
        return bool(_PHONE_RE.match((phone or "").strip()))

    @staticmethod
    def valid_email(email: str) -> bool:
        return bool(_EMAIL_RE.match((email or "").strip()))

    @staticmethod
    def valid_username(username: str) -> bool:
        return bool(_USERNAME_RE.match((username or "").strip()))

    @staticmethod
    def password_strength_error(password: str) -> str:
        """返回复杂度不合规的中文提示, 合规返回空串"""
        pw = password or ""
        if len(pw) < _PW_MIN_LEN:
            return f"密码至少 {_PW_MIN_LEN} 位"
        if len(pw) > _PW_MAX_LEN:
            return f"密码最多 {_PW_MAX_LEN} 位"
        if any(ch.isspace() for ch in pw):
            return "密码不能包含空格"
        classes = sum(1 for r in _PW_CLASS_RES if r.search(pw))
        if classes < _PW_MIN_CLASSES:
            return "密码需包含大写字母、小写字母、数字、特殊字符中的至少三类"
        return ""

    # ---------- 密码 ----------
    @staticmethod
    def _hash_password(password: str, salt_hex: Optional[str] = None) -> tuple[str, str]:
        salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(_SALT_BYTES)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
        return salt.hex(), digest.hex()

    def _verify_password(self, salt_hex: str, password: str, expect_hash: str) -> bool:
        _, actual = self._hash_password(password, salt_hex)
        return secrets.compare_digest(actual, expect_hash)

    # ---------- 用户 ----------
    def register(self, username: str, password: str, phone: str = "", email: str = "") -> dict:
        username = (username or "").strip()
        phone = (phone or "").strip()
        email = (email or "").strip()
        if not self.valid_username(username):
            raise ValueError("账号需 4-20 位, 以字母开头, 仅含字母/数字/下划线")
        pw_err = self.password_strength_error(password)
        if pw_err:
            raise ValueError(pw_err)
        if not phone:
            raise ValueError("手机号必填")
        if not self.valid_phone(phone):
            raise ValueError("手机号格式不正确")
        if email and not self.valid_email(email):
            raise ValueError("邮箱格式不正确")
        if self._phone_taken(phone):
            raise ValueError("该手机号已被注册")
        user_id = "u_" + secrets.token_urlsafe(8)
        salt, pw_hash = self._hash_password(password)
        try:
            with closing(self._conn()) as conn:
                conn.execute(
                    "INSERT INTO users (user_id, username, password_hash, salt, phone, email, created_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (user_id, username, pw_hash, salt, phone, email or None,
                     datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                )
                conn.commit()
        except sqlite3.IntegrityError as e:
            msg = str(e)
            if "phone" in msg:
                raise ValueError("该手机号已被注册")
            raise ValueError(f"用户名 {username} 已存在")
        return {"user_id": user_id, "username": username, "phone": phone,
                "email": email, "phone_bound": True, "role": "user"}

    def _phone_taken(self, phone: str, exclude_user: Optional[str] = None) -> bool:
        with closing(self._conn()) as conn:
            if exclude_user:
                row = conn.execute("SELECT 1 FROM users WHERE phone = ? AND user_id != ?",
                                   (phone, exclude_user)).fetchone()
            else:
                row = conn.execute("SELECT 1 FROM users WHERE phone = ?", (phone,)).fetchone()
        return bool(row)

    def _find_user_name(self, username: str) -> Optional[dict]:
        with closing(self._conn()) as conn:
            row = conn.execute("SELECT user_id, username FROM users WHERE username = ?",
                               (username.strip(),)).fetchone()
        return {"user_id": row[0], "username": row[1]} if row else None

    def login(self, username: str, password: str) -> dict:
        with closing(self._conn()) as conn:
            row = conn.execute(
                "SELECT user_id, username, password_hash, salt, phone, role, status "
                "FROM users WHERE username = ?",
                (username.strip(),)).fetchone()
        if not row or not self._verify_password(row[3], password, row[2]):
            raise ValueError("用户名或密码错误")
        if (row[6] or "active") != "active":
            raise ValueError("该账号已被禁用, 请联系管理员")
        token = secrets.token_urlsafe(32)
        expires = (datetime.now() + timedelta(days=_TOKEN_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with closing(self._conn()) as conn:
            conn.execute("INSERT INTO sessions (token, user_id, expires_at) VALUES (?,?,?)",
                         (token, row[0], expires))
            conn.execute("UPDATE users SET last_login_at = ? WHERE user_id = ?", (now_str, row[0]))
            conn.commit()
        need_bind = not row[4]
        return {"token": token, "user_id": row[0], "username": row[1],
                "need_bind_phone": need_bind, "role": row[5] or "user"}

    def bind_phone(self, user_id: str, phone: str, email: str = "") -> dict:
        phone = (phone or "").strip()
        email = (email or "").strip()
        if not phone:
            raise ValueError("手机号必填")
        if not self.valid_phone(phone):
            raise ValueError("手机号格式不正确")
        if email and not self.valid_email(email):
            raise ValueError("邮箱格式不正确")
        with closing(self._conn()) as conn:
            row = conn.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if not row:
            raise ValueError("用户不存在")
        if self._phone_taken(phone, exclude_user=user_id):
            raise ValueError("该手机号已被注册")
        sets = ["phone = ?"]
        params = [phone]
        if email:
            sets.append("email = ?")
            params.append(email)
        params.append(user_id)
        with closing(self._conn()) as conn:
            conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE user_id = ?", params)
            conn.commit()
        return self.profile(user_id)

    def profile(self, user_id: str) -> Optional[dict]:
        with closing(self._conn()) as conn:
            row = conn.execute(
                "SELECT user_id, username, phone, email, role, status FROM users WHERE user_id = ?",
                (user_id,)).fetchone()
        if not row:
            return None
        return {"user_id": row[0], "username": row[1],
                "phone": row[2] or "", "email": row[3] or "",
                "phone_bound": bool(row[2]), "role": row[4] or "user",
                "status": row[5] or "active"}

    def logout(self, token: str):
        if not token:
            return
        with closing(self._conn()) as conn:
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            conn.commit()

    def get_user(self, token: str) -> Optional[dict]:
        """按 token 解析用户, 校验有效期; 已禁用账号视为无效 (需求101809)"""
        if not token:
            return None
        with closing(self._conn()) as conn:
            row = conn.execute(
                "SELECT s.user_id, u.username, u.phone, u.role, u.status "
                "FROM sessions s JOIN users u ON u.user_id = s.user_id "
                "WHERE s.token = ? AND s.expires_at > ?",
                (token, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            ).fetchone()
        if not row or (row[4] or "active") != "active":
            return None
        return {"user_id": row[0], "username": row[1], "phone_bound": bool(row[2]),
                "role": row[3] or "user"}

    def username_of(self, user_id: str) -> str:
        with closing(self._conn()) as conn:
            row = conn.execute("SELECT username FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return row[0] if row else user_id

    # ---------- 需求101809: 系统后台 —— 账户管理 ----------
    def is_admin(self, user_id: str) -> bool:
        if not user_id or user_id == "default":
            return False
        with closing(self._conn()) as conn:
            row = conn.execute("SELECT role, status FROM users WHERE user_id = ?",
                               (user_id,)).fetchone()
        return bool(row) and (row[0] or "user") == "admin" and (row[1] or "active") == "active"

    def list_users(self, keyword: str = "", limit: int = 50, offset: int = 0) -> dict:
        kw = (keyword or "").strip()
        where, params = "", []
        if kw:
            where = "WHERE username LIKE ? OR phone LIKE ? OR user_id LIKE ? OR email LIKE ?"
            like = f"%{kw}%"
            params = [like, like, like, like]
        with closing(self._conn()) as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM users {where}", params).fetchone()[0]
            rows = conn.execute(
                f"SELECT user_id, username, phone, email, created_at, role, status, last_login_at "
                f"FROM users {where} ORDER BY created_at DESC, username ASC LIMIT ? OFFSET ?",
                params + [limit, offset]).fetchall()
        users = [{
            "user_id": r[0], "username": r[1], "phone": r[2] or "", "email": r[3] or "",
            "created_at": r[4] or "", "role": r[5] or "user", "status": r[6] or "active",
            "last_login_at": r[7] or "",
        } for r in rows]
        return {"total": total, "users": users}

    def user_stats(self) -> dict:
        today = datetime.now().strftime("%Y-%m-%d")
        with closing(self._conn()) as conn:
            total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            active = conn.execute("SELECT COUNT(*) FROM users WHERE status = 'active'").fetchone()[0]
            admins = conn.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'").fetchone()[0]
            new_today = conn.execute(
                "SELECT COUNT(*) FROM users WHERE substr(created_at, 1, 10) = ?", (today,)).fetchone()[0]
            online = conn.execute("SELECT COUNT(DISTINCT user_id) FROM sessions WHERE expires_at > ?",
                                  (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),)).fetchone()[0]
        return {"total": total, "active": active, "banned": total - active,
                "admins": admins, "new_today": new_today, "online": online}

    def set_status(self, user_id: str, status: str) -> bool:
        status = "banned" if status == "banned" else "active"
        with closing(self._conn()) as conn:
            row = conn.execute("SELECT role FROM users WHERE user_id = ?", (user_id,)).fetchone()
            if not row:
                return False
            if status == "banned" and (row[0] or "user") == "admin":
                raise ValueError("不能禁用管理员账号")
            conn.execute("UPDATE users SET status = ? WHERE user_id = ?", (status, user_id))
            if status == "banned":                       # 禁用即失效所有会话
                conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            conn.commit()
        return True

    def set_role(self, user_id: str, role: str) -> bool:
        role = "admin" if role == "admin" else "user"
        with closing(self._conn()) as conn:
            row = conn.execute("SELECT role FROM users WHERE user_id = ?", (user_id,)).fetchone()
            if not row:
                return False
            if (row[0] or "user") == "admin" and role != "admin":
                n = conn.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'").fetchone()[0]
                if n <= 1:
                    raise ValueError("系统至少保留一个管理员")
            conn.execute("UPDATE users SET role = ? WHERE user_id = ?", (role, user_id))
            conn.commit()
        return True

    def reset_password(self, user_id: str, new_password: str) -> bool:
        err = self.password_strength_error(new_password)
        if err:
            raise ValueError(err)
        with closing(self._conn()) as conn:
            row = conn.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,)).fetchone()
            if not row:
                return False
            salt, pw_hash = self._hash_password(new_password)
            conn.execute("UPDATE users SET password_hash = ?, salt = ? WHERE user_id = ?",
                         (pw_hash, salt, user_id))
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            conn.commit()
        return True

    def delete_user(self, user_id: str) -> bool:
        with closing(self._conn()) as conn:
            row = conn.execute("SELECT role FROM users WHERE user_id = ?", (user_id,)).fetchone()
            if not row:
                return False
            if (row[0] or "user") == "admin":
                raise ValueError("不能删除管理员账号")
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
            conn.commit()
        return True


auth_svc = AuthService()
