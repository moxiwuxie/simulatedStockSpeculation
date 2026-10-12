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
            # 手机号全局唯一 (允许多行为 NULL)
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_phone ON users(phone)")
            conn.commit()

    # ---------- 校验 ----------
    @staticmethod
    def valid_phone(phone: str) -> bool:
        return bool(_PHONE_RE.match((phone or "").strip()))

    @staticmethod
    def valid_email(email: str) -> bool:
        return bool(_EMAIL_RE.match((email or "").strip()))

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
        if not username or len(username) < 2:
            raise ValueError("用户名至少 2 个字符")
        if len(password) < 6:
            raise ValueError("密码至少 6 位")
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
                "email": email, "phone_bound": True}

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
                "SELECT user_id, username, password_hash, salt, phone FROM users WHERE username = ?",
                (username.strip(),)).fetchone()
        if not row or not self._verify_password(row[3], password, row[2]):
            raise ValueError("用户名或密码错误")
        token = secrets.token_urlsafe(32)
        expires = (datetime.now() + timedelta(days=_TOKEN_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
        with closing(self._conn()) as conn:
            conn.execute("INSERT INTO sessions (token, user_id, expires_at) VALUES (?,?,?)",
                         (token, row[0], expires))
            conn.commit()
        need_bind = not row[4]
        return {"token": token, "user_id": row[0], "username": row[1],
                "need_bind_phone": need_bind}

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
                "SELECT user_id, username, phone, email FROM users WHERE user_id = ?",
                (user_id,)).fetchone()
        if not row:
            return None
        return {"user_id": row[0], "username": row[1],
                "phone": row[2] or "", "email": row[3] or "",
                "phone_bound": bool(row[2])}

    def logout(self, token: str):
        if not token:
            return
        with closing(self._conn()) as conn:
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            conn.commit()

    def get_user(self, token: str) -> Optional[dict]:
        """按 token 解析用户, 校验有效期"""
        if not token:
            return None
        with closing(self._conn()) as conn:
            row = conn.execute(
                "SELECT s.user_id, u.username, u.phone FROM sessions s JOIN users u ON u.user_id = s.user_id "
                "WHERE s.token = ? AND s.expires_at > ?",
                (token, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            ).fetchone()
        return {"user_id": row[0], "username": row[1], "phone_bound": bool(row[2])} if row else None

    def username_of(self, user_id: str) -> str:
        with closing(self._conn()) as conn:
            row = conn.execute("SELECT username FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return row[0] if row else user_id


auth_svc = AuthService()
