"""Subscribers, their devices, and the answer the app gets about them.

A subscriber is one paying customer: one token, one playlist link, up to
``max_devices`` devices. The four states the IPTV Room app understands -
``ok``, ``warn``, ``block``, ``expired`` - are derived here from three
things the admin controls (status, expiry date, message) plus the device
count, so that the admin screen and the ``/unloop`` endpoint can never
disagree about what a customer is seeing.
"""

from __future__ import annotations

import secrets
import string
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import db

_ALPHABET = string.ascii_lowercase + string.digits
TOKEN_LENGTH = 24


def new_token() -> str:
    while True:
        token = "".join(secrets.choice(_ALPHABET) for _ in range(TOKEN_LENGTH))
        if not db.one("SELECT id FROM subscribers WHERE token=?", (token,)):
            return token


def valid_token(token: str) -> bool:
    return 8 <= len(token) <= 64 and all(c in _ALPHABET + "-_" for c in token)


# --------------------------------------------------------------------- CRUD


def get(subscriber_id: int):
    return db.one(
        "SELECT s.*, p.name AS package_name FROM subscribers s LEFT JOIN packages p ON p.id=s.package_id WHERE s.id=?",
        (subscriber_id,),
    )


def by_token(token: str):
    if not token:
        return None
    return db.one(
        "SELECT s.*, p.name AS package_name FROM subscribers s LEFT JOIN packages p ON p.id=s.package_id WHERE s.token=?",
        (token.strip(),),
    )


def create(name: str, days: int | None, max_devices: int, package_id: int | None = None,
           note: str = "", contact: str = "", token: str = "") -> int:
    now = db.utcnow()
    expires = None
    if days is not None and days > 0:
        expires = db.iso(end_of_day(now + timedelta(days=days)))
    token = token.strip() if token and valid_token(token) and not by_token(token) else new_token()
    cursor = db.execute(
        "INSERT INTO subscribers(token, name, note, contact, status, max_devices, package_id, expires_at, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)",
        (token, name.strip() or "Subscriber", note.strip(), contact.strip(), max(1, int(max_devices)), package_id, expires, db.iso(now), db.iso(now)),
    )
    return int(cursor.lastrowid)


def update(subscriber_id: int, **fields) -> None:
    allowed = {"name", "note", "contact", "status", "message", "action_url", "max_devices", "package_id", "expires_at"}
    sets = []
    params: list = []
    for key, value in fields.items():
        if key not in allowed:
            continue
        sets.append(f"{key}=?")
        params.append(value)
    if not sets:
        return
    sets.append("updated_at=?")
    params.append(db.now_iso())
    params.append(subscriber_id)
    db.execute(f"UPDATE subscribers SET {', '.join(sets)} WHERE id=?", params)


def delete(subscriber_ids: list[int]) -> int:
    if not subscriber_ids:
        return 0
    marks = ",".join("?" * len(subscriber_ids))
    cursor = db.execute(f"DELETE FROM subscribers WHERE id IN ({marks})", subscriber_ids)
    return cursor.rowcount


def regenerate_token(subscriber_id: int) -> str:
    token = new_token()
    with db.transaction() as conn:
        conn.execute("UPDATE subscribers SET token=?, updated_at=? WHERE id=?", (token, db.now_iso(), subscriber_id))
        # A new link is a new start for the device list too.
        conn.execute("DELETE FROM devices WHERE subscriber_id=?", (subscriber_id,))
    return token


def end_of_day(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(hour=23, minute=59, second=59, microsecond=0)


def extend(subscriber_id: int, days: int) -> str | None:
    """Adds days to the expiry - from today if it has already passed."""
    row = get(subscriber_id)
    if row is None:
        return None
    now = db.utcnow()
    current = db.parse_iso(row["expires_at"])
    start = current if current and current > now else now
    new_expiry = end_of_day(start + timedelta(days=days))
    update(subscriber_id, expires_at=db.iso(new_expiry))
    return db.iso(new_expiry)


# ------------------------------------------------------------------ devices


def devices_for(subscriber_id: int) -> list:
    return db.query("SELECT * FROM devices WHERE subscriber_id=? ORDER BY slot", (subscriber_id,))


def remove_device(subscriber_id: int, device_id: str) -> bool:
    cursor = db.execute("DELETE FROM devices WHERE subscriber_id=? AND device_id=?", (subscriber_id, device_id))
    return cursor.rowcount > 0


def claim_slot(conn, subscriber, device_id: str, os_name: str, app_version: str, stable: bool) -> int | None:
    """Registers the device in the first free slot, or returns None if full."""
    now = db.now_iso()
    existing = conn.execute(
        "SELECT slot FROM devices WHERE subscriber_id=? AND device_id=?", (subscriber["id"], device_id)
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE devices SET last_seen_at=?, os=COALESCE(NULLIF(?, ''), os), app_version=COALESCE(NULLIF(?, ''), app_version), stable=? "
            "WHERE subscriber_id=? AND device_id=?",
            (now, os_name, app_version, 1 if stable else 0, subscriber["id"], device_id),
        )
        return int(existing["slot"])
    taken = {int(r["slot"]) for r in conn.execute("SELECT slot FROM devices WHERE subscriber_id=?", (subscriber["id"],))}
    limit = max(1, int(subscriber["max_devices"]))
    if len(taken) >= limit:
        return None
    slot = next(n for n in range(1, limit + 1) if n not in taken)
    conn.execute(
        "INSERT INTO devices(subscriber_id, device_id, slot, os, app_version, stable, first_seen_at, last_seen_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (subscriber["id"], device_id, slot, os_name, app_version, 1 if stable else 0, now, now),
    )
    return slot


# -------------------------------------------------------------------- state


@dataclass
class Standing:
    """What a subscriber's account looks like right now, before devices."""

    state: str  # ok | warn | block | expired
    message: str = ""
    action_url: str = ""
    action_label: str = ""
    days_left: int | None = None
    label: str = "Active"
    tone: str = "ok"
    reason: str = ""


def fill(template: str, **values) -> str:
    try:
        return template.format(**values)
    except (KeyError, IndexError, ValueError):
        return template


def self_service_url(settings: dict, token: str) -> str:
    base = (settings.get("public_base_url") or "").rstrip("/")
    return f"{base}/me/{token}" if base else ""


def standing(subscriber, settings: dict, now: datetime | None = None) -> Standing:
    now = now or db.utcnow()
    expires = db.parse_iso(subscriber["expires_at"])
    days_left = None
    if expires is not None:
        days_left = max(0, int((expires - now).total_seconds() // 86400))
    renew = (subscriber["action_url"] or settings.get("renew_url") or self_service_url(settings, subscriber["token"]) or "").strip()
    support = (subscriber["action_url"] or settings.get("support_url") or settings.get("renew_url") or self_service_url(settings, subscriber["token"]) or "").strip()
    date_text = expires.strftime("%d %B %Y") if expires else ""
    name = subscriber["name"]

    if subscriber["status"] == "blocked":
        return Standing(
            state="block",
            message=subscriber["message"] or fill(settings["msg_blocked"], name=name, date=date_text, days=days_left or 0, limit=subscriber["max_devices"]),
            action_url=support,
            action_label="Scan for help" if support else "",
            days_left=days_left,
            label="Blocked",
            tone="bad",
            reason="Suspended by the admin",
        )
    if expires is not None and expires <= now:
        return Standing(
            state="expired",
            message=fill(settings["msg_expired"], name=name, date=date_text, days=0, limit=subscriber["max_devices"]),
            action_url=renew,
            action_label=settings.get("action_label_renew", "") if renew else "",
            days_left=0,
            label="Expired",
            tone="bad",
            reason=f"Expired on {date_text}",
        )
    if subscriber["status"] == "warned":
        return Standing(
            state="warn",
            message=subscriber["message"] or fill(settings["msg_expiring"], name=name, date=date_text, days=days_left if days_left is not None else 0, limit=subscriber["max_devices"]),
            action_url=renew,
            action_label=settings.get("action_label_renew", "") if renew else "",
            days_left=days_left,
            label="Warning",
            tone="warn",
            reason="Warning set by the admin",
        )
    warn_days = int(settings.get("warn_days") or 0)
    if days_left is not None and warn_days > 0 and days_left < warn_days:
        return Standing(
            state="warn",
            message=fill(settings["msg_expiring"], name=name, date=date_text, days=days_left, limit=subscriber["max_devices"]),
            action_url=renew,
            action_label=settings.get("action_label_renew", "") if renew else "",
            days_left=days_left,
            label="Expiring",
            tone="warn",
            reason=f"Ends in {days_left} day(s)",
        )
    return Standing(
        state="ok",
        days_left=days_left,
        label="Active",
        tone="ok",
        reason="Never expires" if expires is None else f"Until {date_text}",
    )


def list_filter(search: str, state: str) -> tuple[str, list]:
    where = ["1=1"]
    params: list = []
    if search:
        like = f"%{search.strip()}%"
        where.append("(s.name LIKE ? OR s.note LIKE ? OR s.contact LIKE ? OR s.token LIKE ?)")
        params.extend([like, like, like, like])
    now = db.now_iso()
    if state == "blocked":
        where.append("s.status='blocked'")
    elif state == "expired":
        where.append("s.status != 'blocked' AND s.expires_at IS NOT NULL AND s.expires_at <= ?")
        params.append(now)
    elif state == "active":
        where.append("s.status != 'blocked' AND (s.expires_at IS NULL OR s.expires_at > ?)")
        params.append(now)
    elif state == "expiring":
        soon = db.iso(db.utcnow() + timedelta(days=int(db.get_settings().get("warn_days") or 5)))
        where.append("s.status != 'blocked' AND s.expires_at IS NOT NULL AND s.expires_at > ? AND s.expires_at <= ?")
        params.extend([now, soon])
    return " AND ".join(where), params


def stats(settings: dict) -> dict:
    now = db.utcnow()
    soon = db.iso(now + timedelta(days=int(settings.get("warn_days") or 5)))
    now_iso = db.iso(now)
    return {
        "total": db.scalar("SELECT COUNT(*) FROM subscribers") or 0,
        "active": db.scalar(
            "SELECT COUNT(*) FROM subscribers WHERE status != 'blocked' AND (expires_at IS NULL OR expires_at > ?)", (now_iso,)
        ) or 0,
        "expiring": db.scalar(
            "SELECT COUNT(*) FROM subscribers WHERE status != 'blocked' AND expires_at IS NOT NULL AND expires_at > ? AND expires_at <= ?",
            (now_iso, soon),
        ) or 0,
        "expired": db.scalar(
            "SELECT COUNT(*) FROM subscribers WHERE status != 'blocked' AND expires_at IS NOT NULL AND expires_at <= ?", (now_iso,)
        ) or 0,
        "blocked": db.scalar("SELECT COUNT(*) FROM subscribers WHERE status='blocked'") or 0,
        "devices": db.scalar("SELECT COUNT(*) FROM devices") or 0,
        "watching": db.scalar(
            "SELECT COUNT(DISTINCT subscriber_id || ':' || session_id) FROM activity WHERE at > ? AND state IN ('ok', 'warn') AND session_id != ''",
            (db.iso(now - timedelta(minutes=15)),),
        ) or 0,
    }
