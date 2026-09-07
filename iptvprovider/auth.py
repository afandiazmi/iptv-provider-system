"""Admin accounts: password hashing and a small brake on guessing.

scrypt from the standard library, so there is nothing to compile and no
dependency to audit. The login brake is per process and in memory - enough
to make guessing slow, without a table that has to be pruned.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time

from . import db

_attempts: dict[str, list[float]] = {}
_attempts_lock = threading.Lock()
MAX_ATTEMPTS = 8
WINDOW_SECONDS = 600


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def check_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_b64, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
    except (ValueError, TypeError):
        return False
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return hmac.compare_digest(digest, expected)


def has_admins() -> bool:
    return bool(db.scalar("SELECT COUNT(*) FROM admins"))


def create_admin(username: str, password: str) -> int:
    cursor = db.execute(
        "INSERT INTO admins(username, password_hash, created_at) VALUES (?, ?, ?)",
        (username.strip(), hash_password(password), db.now_iso()),
    )
    return int(cursor.lastrowid)


def authenticate(username: str, password: str):
    row = db.one("SELECT * FROM admins WHERE username=?", (username.strip(),))
    if row is None:
        # Same cost as a real check, so a missing user is not faster to probe.
        hash_password(password)
        return None
    if not check_password(password, row["password_hash"]):
        return None
    db.execute("UPDATE admins SET last_login_at=? WHERE id=?", (db.now_iso(), row["id"]))
    return row


def change_password(admin_id: int, password: str) -> None:
    db.execute("UPDATE admins SET password_hash=? WHERE id=?", (hash_password(password), admin_id))


def too_many_attempts(key: str) -> bool:
    now = time.time()
    with _attempts_lock:
        times = [t for t in _attempts.get(key, []) if now - t < WINDOW_SECONDS]
        _attempts[key] = times
        return len(times) >= MAX_ATTEMPTS


def record_attempt(key: str) -> None:
    with _attempts_lock:
        _attempts.setdefault(key, []).append(time.time())


def clear_attempts(key: str) -> None:
    with _attempts_lock:
        _attempts.pop(key, None)
