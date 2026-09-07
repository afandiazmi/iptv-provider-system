"""SQLite storage: schema, connections and the settings table.

One file, WAL mode, one connection per thread. The schema is created on first
run and upgraded in place with the ``schema_version`` row, so a panel that has
been running for a year can be updated by replacing the code and restarting.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from . import config

_local = threading.local()
_init_lock = threading.Lock()
_initialised = False

DB_PATH: Path = config.DATA_DIR / "provider.db"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    """A sortable UTC timestamp with second precision, or None."""
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def now_iso() -> str:
    return iso(utcnow())  # type: ignore[return-value]


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


# ---------------------------------------------------------------- connection


def connect() -> sqlite3.Connection:
    """The calling thread's connection, opened on first use."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(DB_PATH),
            timeout=30,
            isolation_level=None,  # autocommit; transactions are explicit
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        _local.conn = conn
    return conn


class transaction:
    """``with transaction() as conn:`` - BEGIN IMMEDIATE, commit or roll back."""

    def __enter__(self) -> sqlite3.Connection:
        self.conn = connect()
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.conn.execute("COMMIT")
        else:
            self.conn.execute("ROLLBACK")


def query(sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    return connect().execute(sql, tuple(params)).fetchall()


def one(sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
    return connect().execute(sql, tuple(params)).fetchone()


def scalar(sql: str, params: Iterable[Any] = ()) -> Any:
    row = one(sql, params)
    return None if row is None else row[0]


def execute(sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
    return connect().execute(sql, tuple(params))


# -------------------------------------------------------------------- schema

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS admins (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    last_login_at TEXT
);

-- One upstream playlist: a URL that is fetched, a file that was uploaded, or
-- the built-in "Manual" source that holds channels typed in by hand.
CREATE TABLE IF NOT EXISTS sources (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    name               TEXT NOT NULL,
    kind               TEXT NOT NULL CHECK (kind IN ('url', 'upload', 'manual')),
    url                TEXT NOT NULL DEFAULT '',
    user_agent         TEXT NOT NULL DEFAULT '',
    auto_refresh_hours REAL NOT NULL DEFAULT 0,
    enabled            INTEGER NOT NULL DEFAULT 1,
    position           INTEGER NOT NULL DEFAULT 0,
    header_attrs       TEXT NOT NULL DEFAULT '{}',
    epg_urls           TEXT NOT NULL DEFAULT '[]',
    channel_count      INTEGER NOT NULL DEFAULT 0,
    hidden_count       INTEGER NOT NULL DEFAULT 0,
    last_fetched_at    TEXT,
    last_success_at    TEXT,
    last_error         TEXT NOT NULL DEFAULT '',
    last_bytes         INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);

-- Every entry of every source, as it currently stands after the admin's own
-- edits have been applied. Rebuilt from scratch whenever a source refreshes.
CREATE TABLE IF NOT EXISTS channels (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id   INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL,
    match_key   TEXT NOT NULL,
    name        TEXT NOT NULL,
    url         TEXT NOT NULL,
    group_name  TEXT NOT NULL DEFAULT '',
    tvg_id      TEXT NOT NULL DEFAULT '',
    tvg_logo    TEXT NOT NULL DEFAULT '',
    duration    TEXT NOT NULL DEFAULT '-1',
    attrs       TEXT NOT NULL DEFAULT '{}',
    extras      TEXT NOT NULL DEFAULT '[]',
    hidden      INTEGER NOT NULL DEFAULT 0,
    edited      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS channels_source_pos ON channels(source_id, position);
CREATE INDEX IF NOT EXISTS channels_source_key ON channels(source_id, match_key);
CREATE INDEX IF NOT EXISTS channels_group ON channels(source_id, group_name);
CREATE INDEX IF NOT EXISTS channels_hidden ON channels(hidden);

-- The admin's edits and deletions, kept apart from the fetched rows so that
-- they survive a refresh. Keyed by the same match_key the parser computes.
CREATE TABLE IF NOT EXISTS channel_overrides (
    source_id  INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    match_key  TEXT NOT NULL,
    fields     TEXT NOT NULL DEFAULT '{}',
    hidden     INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (source_id, match_key)
);

-- A bundle of sources a subscriber may see. NULL package on a subscriber
-- means every enabled source.
CREATE TABLE IF NOT EXISTS packages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    source_ids  TEXT NOT NULL DEFAULT '[]',
    groups      TEXT NOT NULL DEFAULT '[]',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS subscribers (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    token        TEXT NOT NULL UNIQUE,
    name         TEXT NOT NULL,
    note         TEXT NOT NULL DEFAULT '',
    contact      TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'warned', 'blocked')),
    message      TEXT NOT NULL DEFAULT '',
    action_url   TEXT NOT NULL DEFAULT '',
    max_devices  INTEGER NOT NULL DEFAULT 2,
    package_id   INTEGER REFERENCES packages(id) ON DELETE SET NULL,
    expires_at   TEXT,
    last_seen_at TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS subscribers_status ON subscribers(status);
CREATE INDEX IF NOT EXISTS subscribers_expires ON subscribers(expires_at);

CREATE TABLE IF NOT EXISTS devices (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    subscriber_id INTEGER NOT NULL REFERENCES subscribers(id) ON DELETE CASCADE,
    device_id     TEXT NOT NULL,
    slot          INTEGER NOT NULL,
    label         TEXT NOT NULL DEFAULT '',
    os            TEXT NOT NULL DEFAULT '',
    app_version   TEXT NOT NULL DEFAULT '',
    stable        INTEGER NOT NULL DEFAULT 1,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    UNIQUE (subscriber_id, device_id)
);

CREATE TABLE IF NOT EXISTS activity (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    at            TEXT NOT NULL,
    subscriber_id INTEGER,
    token         TEXT NOT NULL DEFAULT '',
    device_id     TEXT NOT NULL DEFAULT '',
    session_id    TEXT NOT NULL DEFAULT '',
    reason        TEXT NOT NULL DEFAULT '',
    state         TEXT NOT NULL DEFAULT '',
    detail        TEXT NOT NULL DEFAULT '',
    ip            TEXT NOT NULL DEFAULT '',
    os            TEXT NOT NULL DEFAULT '',
    app_version   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS activity_at ON activity(at);
CREATE INDEX IF NOT EXISTS activity_subscriber ON activity(subscriber_id, at);
"""

SCHEMA_VERSION = 1


def init() -> None:
    """Creates the schema, the secret key and the Manual source. Idempotent."""
    global _initialised
    with _init_lock:
        if _initialised:
            return
        conn = connect()
        conn.executescript(SCHEMA)
        version = int(scalar("SELECT value FROM meta WHERE key='schema_version'") or 0)
        if version < SCHEMA_VERSION:
            execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
        if not scalar("SELECT value FROM meta WHERE key='secret_key'"):
            execute(
                "INSERT INTO meta(key, value) VALUES ('secret_key', ?)",
                (secrets.token_urlsafe(48),),
            )
        if not one("SELECT id FROM sources WHERE kind='manual'"):
            now = now_iso()
            execute(
                "INSERT INTO sources(name, kind, position, created_at, updated_at) "
                "VALUES ('Manual channels', 'manual', 999999, ?, ?)",
                (now, now),
            )
        _initialised = True


def secret_key() -> str:
    return str(scalar("SELECT value FROM meta WHERE key='secret_key'"))


# ------------------------------------------------------------------ settings

DEFAULT_SETTINGS: dict[str, Any] = {
    # Where this panel is reachable from the outside. Must be https for the
    # IPTV Room app to honour the authorisation line at all.
    "public_base_url": "",
    "provider_name": "My IPTV",
    "playlist_name": "",
    "epg_urls": "",
    # The #UNLOOP-AUTH directive attributes.
    "auth_poll": 300,
    "auth_grace": 3600,
    "auth_on_error": "allow",
    # When to start warning about an expiry, in days. 0 disables.
    "warn_days": 5,
    # Words shown on the television. {days}, {date}, {name}, {limit} are filled in.
    "msg_expiring": "Your subscription ends in {days} day(s). Renew to keep watching.",
    "msg_expired": "Your subscription ended on {date}. Renew to keep watching.",
    "msg_blocked": "This account has been suspended. Contact your provider.",
    "msg_device_limit": "All {limit} of your devices are in use. Remove one to watch here.",
    "msg_not_found": "This playlist is no longer active.",
    # Where the QR code on a refusal screen points. Blank = this panel's own
    # self-service page for the subscriber.
    "renew_url": "",
    "support_url": "",
    "action_label_renew": "Scan to renew",
    "action_label_devices": "Scan to manage devices",
    # Default number of devices for a new subscriber.
    "default_max_devices": 2,
    "default_days": 30,
    # What blocked or expired subscribers receive from the playlist URL:
    # "empty" (a header only) or "full" (every channel, and the app decides).
    "playlist_when_refused": "empty",
    # Advanced: headers the app should add to every stream request, as JSON.
    "stream_headers": "",
    "stream_headers_ttl": 600,
    # Self-service page: may a subscriber remove their own devices?
    "self_service_devices": 1,
    "self_service_enabled": 1,
    "default_user_agent": "",
}


def get_settings() -> dict[str, Any]:
    out = dict(DEFAULT_SETTINGS)
    for row in query("SELECT key, value FROM settings"):
        key = row["key"]
        if key not in out:
            continue
        default = out[key]
        raw = row["value"]
        try:
            if isinstance(default, bool):
                out[key] = raw in ("1", "true", "True")
            elif isinstance(default, int):
                out[key] = int(raw)
            elif isinstance(default, float):
                out[key] = float(raw)
            else:
                out[key] = raw
        except (TypeError, ValueError):
            pass
    return out


def set_settings(values: dict[str, Any]) -> None:
    with transaction() as conn:
        for key, value in values.items():
            if key not in DEFAULT_SETTINGS:
                continue
            if isinstance(value, bool):
                value = "1" if value else "0"
            conn.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )


def bump_playlist_version() -> None:
    """Invalidates every cached playlist body (see playlist.py)."""
    execute(
        "INSERT INTO meta(key, value) VALUES ('playlist_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(secrets.token_hex(8)),),
    )


def playlist_version() -> str:
    return str(scalar("SELECT value FROM meta WHERE key='playlist_version'") or "0")


def loads(text: str | None, fallback: Any) -> Any:
    try:
        return json.loads(text) if text else fallback
    except (TypeError, ValueError):
        return fallback


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
