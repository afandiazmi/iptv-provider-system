"""Sources and channels: importing, refreshing, editing, hiding.

The rule that shapes this module: a refresh replaces every fetched row of a
source, and the admin's own work is kept in ``channel_overrides`` so that it
is re-applied on top. Deleting a channel from a fetched source therefore
means hiding it - a tombstone that stops it coming back next refresh -
whereas a channel in the Manual source is simply deleted.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from pathlib import Path

from . import config, db, fetcher, m3u

EDITABLE = ("name", "url", "group_name", "tvg_id", "tvg_logo", "duration", "extras")
_DURATION = re.compile(r"^[-+]?\d+(\.\d+)?$")


def clean_duration(value) -> str:
    """``-1`` unless the value is a plain number - ``#EXTINF:abc`` breaks players."""
    text = str(value if value is not None else "").strip()
    return text if _DURATION.match(text) else "-1"

_refresh_locks: dict[int, threading.Lock] = {}
_refresh_locks_guard = threading.Lock()


def _lock_for(source_id: int) -> threading.Lock:
    with _refresh_locks_guard:
        return _refresh_locks.setdefault(source_id, threading.Lock())


def raw_path(source_id: int) -> Path:
    folder = config.DATA_DIR / "sources"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{source_id}.m3u"


@dataclass
class ImportResult:
    channels: int
    hidden: int
    groups: int
    warnings: list[str]


# ------------------------------------------------------------------ sources


def list_sources() -> list:
    return db.query("SELECT * FROM sources ORDER BY kind='manual', position, id")


def get_source(source_id: int):
    return db.one("SELECT * FROM sources WHERE id=?", (source_id,))


def manual_source():
    return db.one("SELECT * FROM sources WHERE kind='manual'")


def find_source_by_url(url: str):
    return db.one("SELECT * FROM sources WHERE kind='url' AND url=?", (url.strip(),))


def create_source(name: str, kind: str, url: str = "", user_agent: str = "", auto_refresh_hours: float = 0) -> int:
    now = db.now_iso()
    position = int(db.scalar("SELECT COALESCE(MAX(position), 0) + 1 FROM sources WHERE kind != 'manual'") or 1)
    cursor = db.execute(
        "INSERT INTO sources(name, kind, url, user_agent, auto_refresh_hours, position, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (name.strip() or "Untitled", kind, url.strip(), user_agent.strip(), float(auto_refresh_hours or 0), position, now, now),
    )
    return int(cursor.lastrowid)


def update_source(source_id: int, **fields) -> None:
    allowed = {"name", "url", "user_agent", "auto_refresh_hours", "enabled", "position"}
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
    params.append(source_id)
    db.execute(f"UPDATE sources SET {', '.join(sets)} WHERE id=?", params)
    db.bump_playlist_version()


def delete_source(source_id: int) -> bool:
    source = get_source(source_id)
    if source is None or source["kind"] == "manual":
        return False
    with db.transaction() as conn:
        conn.execute("DELETE FROM sources WHERE id=?", (source_id,))
    try:
        raw_path(source_id).unlink(missing_ok=True)
    except OSError:
        pass
    db.bump_playlist_version()
    return True


# ------------------------------------------------------------------ import


def _apply_override(entry: m3u.Entry, fields: dict) -> None:
    if "name" in fields:
        entry.name = str(fields["name"])
    if "url" in fields:
        entry.url = str(fields["url"])
    if "duration" in fields:
        entry.duration = clean_duration(fields["duration"])
    if "extras" in fields:
        entry.extras = [str(line) for line in fields["extras"] if str(line).strip()]
    if "group_name" in fields:
        m3u.set_attr(entry.attrs, "group-title", str(fields["group_name"]))
    if "tvg_id" in fields:
        m3u.set_attr(entry.attrs, "tvg-id", str(fields["tvg_id"]))
    if "tvg_logo" in fields:
        m3u.set_attr(entry.attrs, "tvg-logo", str(fields["tvg_logo"]))


def import_text(source_id: int, text: str, keep_raw: bool = True) -> ImportResult:
    """Replaces every channel of a source with the entries in *text*."""
    source = get_source(source_id)
    if source is None:
        raise ValueError("No such source")
    playlist = m3u.parse(text)

    overrides = {
        row["match_key"]: (db.loads(row["fields"], {}), bool(row["hidden"]))
        for row in db.query("SELECT match_key, fields, hidden FROM channel_overrides WHERE source_id=?", (source_id,))
    }

    rows = []
    hidden_count = 0
    groups: set[str] = set()
    for position, entry in enumerate(playlist.entries):
        fields, hidden = overrides.get(entry.match_key, ({}, False))
        edited = bool(fields)
        if fields:
            _apply_override(entry, fields)
        if hidden:
            hidden_count += 1
        groups.add(entry.group)
        rows.append(
            (
                source_id,
                position,
                entry.match_key,
                entry.name,
                entry.url,
                entry.group,
                entry.tvg_id,
                entry.tvg_logo,
                entry.duration,
                db.dumps(entry.attrs),
                db.dumps(entry.extras),
                1 if hidden else 0,
                1 if edited else 0,
            )
        )

    now = db.now_iso()
    with db.transaction() as conn:
        conn.execute("DELETE FROM channels WHERE source_id=?", (source_id,))
        conn.executemany(
            "INSERT INTO channels(source_id, position, match_key, name, url, group_name, tvg_id, tvg_logo, "
            "duration, attrs, extras, hidden, edited) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.execute(
            "UPDATE sources SET channel_count=?, hidden_count=?, header_attrs=?, epg_urls=?, "
            "last_success_at=?, last_fetched_at=?, last_error='', last_bytes=?, updated_at=? WHERE id=?",
            (
                len(rows),
                hidden_count,
                db.dumps(playlist.header_attrs),
                db.dumps(playlist.epg_urls),
                now,
                now,
                len(text.encode("utf-8", errors="replace")),
                now,
                source_id,
            ),
        )
    if keep_raw:
        try:
            raw_path(source_id).write_text(text, encoding="utf-8")
        except OSError:
            pass
    db.bump_playlist_version()
    return ImportResult(channels=len(rows), hidden=hidden_count, groups=len(groups), warnings=playlist.warnings)


def record_failure(source_id: int, message: str) -> None:
    db.execute(
        "UPDATE sources SET last_fetched_at=?, last_error=?, updated_at=? WHERE id=?",
        (db.now_iso(), message[:500], db.now_iso(), source_id),
    )


def refresh_source(source_id: int) -> tuple[bool, str]:
    """Fetches a URL source again and replaces its channels."""
    source = get_source(source_id)
    if source is None:
        return False, "No such source."
    if source["kind"] != "url" or not source["url"]:
        return False, "Only URL sources can be refreshed. Upload a new file instead."
    lock = _lock_for(source_id)
    if not lock.acquire(blocking=False):
        return False, "A refresh of this source is already running."
    try:
        settings = db.get_settings()
        agent = source["user_agent"] or settings.get("default_user_agent", "")
        try:
            fetched = fetcher.fetch(source["url"], user_agent=agent)
        except fetcher.FetchError as error:
            record_failure(source_id, str(error))
            return False, str(error)
        if not fetcher.looks_like_playlist(fetched.text):
            record_failure(source_id, "The URL did not return a playlist.")
            return False, "The URL did not return an M3U playlist (no #EXTM3U, #EXTINF or stream URLs found)."
        result = import_text(source_id, fetched.text)
        note = f"{result.channels} channels in {result.groups} groups"
        if result.warnings:
            note += " - " + " ".join(result.warnings)
        return True, note
    finally:
        lock.release()


def rebuild_from_raw(source_id: int) -> tuple[bool, str]:
    """Re-imports the last stored copy - after overrides were cleared, say."""
    path = raw_path(source_id)
    if not path.exists():
        return False, "No stored copy of this source to rebuild from."
    result = import_text(source_id, path.read_text(encoding="utf-8", errors="replace"), keep_raw=False)
    return True, f"{result.channels} channels rebuilt."


def clear_overrides(source_id: int) -> tuple[bool, str]:
    db.execute("DELETE FROM channel_overrides WHERE source_id=?", (source_id,))
    return rebuild_from_raw(source_id)


# ----------------------------------------------------------------- channels


def get_channel(channel_id: int):
    return db.one(
        "SELECT c.*, s.name AS source_name, s.kind AS source_kind FROM channels c "
        "JOIN sources s ON s.id=c.source_id WHERE c.id=?",
        (channel_id,),
    )


def _write_override(conn, source_id: int, match_key: str, fields: dict | None = None, hidden: bool | None = None) -> None:
    row = conn.execute(
        "SELECT fields, hidden FROM channel_overrides WHERE source_id=? AND match_key=?",
        (source_id, match_key),
    ).fetchone()
    current_fields = db.loads(row["fields"], {}) if row else {}
    current_hidden = bool(row["hidden"]) if row else False
    if fields is not None:
        current_fields.update(fields)
    if hidden is not None:
        current_hidden = hidden
    conn.execute(
        "INSERT INTO channel_overrides(source_id, match_key, fields, hidden, updated_at) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(source_id, match_key) DO UPDATE SET fields=excluded.fields, hidden=excluded.hidden, "
        "updated_at=excluded.updated_at",
        (source_id, match_key, db.dumps(current_fields), 1 if current_hidden else 0, db.now_iso()),
    )


def update_channel(channel_id: int, fields: dict) -> bool:
    channel = get_channel(channel_id)
    if channel is None:
        return False
    clean: dict = {}
    for key in EDITABLE:
        if key not in fields:
            continue
        value = fields[key]
        if key == "extras":
            if isinstance(value, str):
                value = [line.strip() for line in value.splitlines() if line.strip()]
            value = [str(line) for line in value if str(line).startswith("#")]
        elif key == "duration":
            value = clean_duration(value)
        else:
            value = str(value).strip()
        clean[key] = value
    if not clean:
        return False

    entry = m3u.Entry(
        name=channel["name"],
        url=channel["url"],
        duration=channel["duration"],
        attrs=db.loads(channel["attrs"], {}),
        extras=db.loads(channel["extras"], []),
    )
    _apply_override(entry, clean)
    if not entry.name.strip():
        entry.name = channel["name"]
    if not entry.url.strip():
        entry.url = channel["url"]

    with db.transaction() as conn:
        conn.execute(
            "UPDATE channels SET name=?, url=?, group_name=?, tvg_id=?, tvg_logo=?, duration=?, attrs=?, extras=?, edited=1 "
            "WHERE id=?",
            (
                entry.name,
                entry.url,
                entry.group,
                entry.tvg_id,
                entry.tvg_logo,
                entry.duration,
                db.dumps(entry.attrs),
                db.dumps(entry.extras),
                channel_id,
            ),
        )
        if channel["source_kind"] != "manual":
            _write_override(conn, channel["source_id"], channel["match_key"], fields=clean)
    db.bump_playlist_version()
    return True


def set_hidden(channel_ids: list[int], hidden: bool) -> int:
    """Hides or restores channels. Manual channels are deleted rather than hidden."""
    if not channel_ids:
        return 0
    manual = manual_source()
    manual_id = manual["id"] if manual else -1
    changed = 0
    with db.transaction() as conn:
        for chunk_start in range(0, len(channel_ids), 500):
            chunk = channel_ids[chunk_start:chunk_start + 500]
            marks = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"SELECT id, source_id, match_key FROM channels WHERE id IN ({marks})", chunk
            ).fetchall()
            for row in rows:
                if row["source_id"] == manual_id:
                    if hidden:
                        conn.execute("DELETE FROM channels WHERE id=?", (row["id"],))
                        changed += 1
                    continue
                conn.execute("UPDATE channels SET hidden=? WHERE id=?", (1 if hidden else 0, row["id"]))
                _write_override(conn, row["source_id"], row["match_key"], hidden=hidden)
                changed += 1
        _recount(conn)
    db.bump_playlist_version()
    return changed


def set_hidden_by_filter(source_id: int | None, hidden: bool, group: str | None = None, search: str = "") -> int:
    """Select-all across pages: every channel matching the current filter."""
    where, params = channel_filter(source_id, group, search, include_hidden=not hidden)
    ids = [row["id"] for row in db.query(f"SELECT c.id FROM channels c WHERE {where}", params)]
    return set_hidden(ids, hidden)


def _recount(conn) -> None:
    conn.execute(
        "UPDATE sources SET channel_count=(SELECT COUNT(*) FROM channels WHERE source_id=sources.id), "
        "hidden_count=(SELECT COUNT(*) FROM channels WHERE source_id=sources.id AND hidden=1)"
    )


def add_manual_channel(fields: dict) -> int:
    manual = manual_source()
    entry = m3u.Entry(name=str(fields.get("name", "")).strip() or "New channel", url=str(fields.get("url", "")).strip())
    extras = fields.get("extras", [])
    if isinstance(extras, str):
        extras = [line.strip() for line in extras.splitlines() if line.strip().startswith("#")]
    entry.extras = list(extras)
    entry.duration = clean_duration(fields.get("duration", "-1"))
    m3u.set_attr(entry.attrs, "group-title", str(fields.get("group_name", "")).strip())
    m3u.set_attr(entry.attrs, "tvg-id", str(fields.get("tvg_id", "")).strip())
    m3u.set_attr(entry.attrs, "tvg-logo", str(fields.get("tvg_logo", "")).strip())
    with db.transaction() as conn:
        position = int(conn.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM channels WHERE source_id=?", (manual["id"],)).fetchone()[0])
        cursor = conn.execute(
            "INSERT INTO channels(source_id, position, match_key, name, url, group_name, tvg_id, tvg_logo, duration, attrs, extras, hidden, edited) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1)",
            (
                manual["id"],
                position,
                f"manual:{position}:{db.now_iso()}",
                entry.name,
                entry.url,
                entry.group,
                entry.tvg_id,
                entry.tvg_logo,
                entry.duration,
                db.dumps(entry.attrs),
                db.dumps(entry.extras),
            ),
        )
        _recount(conn)
    db.bump_playlist_version()
    return int(cursor.lastrowid)


def channel_filter(source_id: int | None, group: str | None, search: str, include_hidden: bool = True, only_hidden: bool = False):
    where = ["1=1"]
    params: list = []
    if source_id:
        where.append("c.source_id=?")
        params.append(source_id)
    if group is not None and group != "":
        where.append("c.group_name=?")
        params.append(group)
    if search:
        like = f"%{search.strip()}%"
        where.append("(c.name LIKE ? OR c.url LIKE ? OR c.tvg_id LIKE ? OR c.group_name LIKE ?)")
        params.extend([like, like, like, like])
    if only_hidden:
        where.append("c.hidden=1")
    elif not include_hidden:
        where.append("c.hidden=0")
    return " AND ".join(where), params


def groups_for(source_id: int | None) -> list:
    if source_id:
        return db.query(
            "SELECT group_name, COUNT(*) AS n, SUM(hidden) AS hidden FROM channels WHERE source_id=? "
            "GROUP BY group_name ORDER BY MIN(position)",
            (source_id,),
        )
    return db.query(
        "SELECT group_name, COUNT(*) AS n, SUM(hidden) AS hidden FROM channels GROUP BY group_name ORDER BY group_name"
    )


def stats() -> dict:
    return {
        "sources": db.scalar("SELECT COUNT(*) FROM sources WHERE kind != 'manual'") or 0,
        "channels": db.scalar("SELECT COUNT(*) FROM channels WHERE hidden=0") or 0,
        "hidden": db.scalar("SELECT COUNT(*) FROM channels WHERE hidden=1") or 0,
        "groups": db.scalar("SELECT COUNT(DISTINCT group_name) FROM channels WHERE hidden=0") or 0,
        "errors": db.scalar("SELECT COUNT(*) FROM sources WHERE last_error != ''") or 0,
    }


def sources_due() -> list:
    """URL sources whose auto-refresh interval has elapsed."""
    now = db.utcnow()
    due = []
    for row in db.query("SELECT * FROM sources WHERE kind='url' AND enabled=1 AND auto_refresh_hours > 0"):
        last = db.parse_iso(row["last_fetched_at"])
        if last is None:
            due.append(row)
            continue
        if (now - last).total_seconds() >= float(row["auto_refresh_hours"]) * 3600:
            due.append(row)
    return due
