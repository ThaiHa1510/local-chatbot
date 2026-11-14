"""Local authentication and role based access control."""
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import sqlite3

ROLES = {"viewer", "agent", "admin"}
PERMISSIONS = {
    "chat": ROLES,
    "ticket.read": {"agent", "admin"},
    "ticket.write": {"agent", "admin"},
    "notification.prepare": {"agent", "admin"},
    "notification.execute": {"admin"},
    "trace.read": {"admin"},
    "user.manage": {"admin"},
}


def allowed(role, permission):
    return role in PERMISSIONS.get(permission, set())


def require(role, permission):
    if not allowed(role, permission):
        raise PermissionError("Vai trò không có quyền thực hiện thao tác này.")


class AuthStore:
    def __init__(self, db_path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS users(
                username TEXT PRIMARY KEY, password_hash TEXT NOT NULL,
                salt TEXT NOT NULL, role TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1);
              CREATE TABLE IF NOT EXISTS sessions(
                token_hash TEXT PRIMARY KEY, username TEXT NOT NULL,
                csrf TEXT NOT NULL, expires_at TEXT NOT NULL);
            """)

    def connect(self):
        return sqlite3.connect(self.db_path)

    @staticmethod
    def _hash(password, salt):
        return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 310000).hex()

    def create_user(self, username, password, role="viewer"):
        username = (username or "").strip().lower()
        if not (3 <= len(username) <= 64) or len(password or "") < 10 or role not in ROLES:
            raise ValueError("Tên đăng nhập, mật khẩu hoặc vai trò không hợp lệ.")
        salt = secrets.token_hex(16)
        with self.connect() as db:
            db.execute("INSERT INTO users(username,password_hash,salt,role) VALUES(?,?,?,?)",
                       (username, self._hash(password, salt), salt, role))
        return {"username": username, "role": role, "active": True}

    def bootstrap(self):
        with self.connect() as db:
            if db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
                return None
        password = os.environ.get("SUPPORT_ADMIN_PASSWORD") or secrets.token_urlsafe(14)
        username = os.environ.get("SUPPORT_ADMIN_USERNAME", "admin")
        self.create_user(username, password, "admin")
        return username, password

    def login(self, username, password, hours=8):
        with self.connect() as db:
            row = db.execute("SELECT password_hash,salt,role,active FROM users WHERE username=?",
                             ((username or "").strip().lower(),)).fetchone()
            if not row or not row[3] or not hmac.compare_digest(row[0], self._hash(password or "", row[1])):
                raise PermissionError("Sai tên đăng nhập hoặc mật khẩu.")
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
            expires = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
            db.execute("INSERT INTO sessions VALUES(?,?,?,?)",
                       (hashlib.sha256(token.encode()).hexdigest(), username.lower(), csrf, expires))
        return token, {"username": username.lower(), "role": row[2], "csrf": csrf}

    def session(self, token):
        if not token:
            return None
        key = hashlib.sha256(token.encode()).hexdigest()
        with self.connect() as db:
            row = db.execute("""SELECT s.username,u.role,s.csrf,s.expires_at,u.active
                FROM sessions s JOIN users u ON u.username=s.username WHERE s.token_hash=?""", (key,)).fetchone()
        if not row or not row[4] or datetime.fromisoformat(row[3]) <= datetime.now(timezone.utc):
            return None
        return {"username": row[0], "role": row[1], "csrf": row[2]}

    def logout(self, token):
        if token:
            with self.connect() as db:
                db.execute("DELETE FROM sessions WHERE token_hash=?",
                           (hashlib.sha256(token.encode()).hexdigest(),))

    def list_users(self):
        with self.connect() as db:
            return [{"username": r[0], "role": r[1], "active": bool(r[2])}
                    for r in db.execute("SELECT username,role,active FROM users ORDER BY username")]
