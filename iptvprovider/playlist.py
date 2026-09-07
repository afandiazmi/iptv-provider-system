"""Building the playlist one subscriber receives.

The entries are the same for everybody on the same package, so that part is
rendered once per package and cached until anything in the library changes;
only the header and the ``#UNLOOP-AUTH`` line are written per subscriber.
"""

from __future__ import annotations

import threading

from . import db, m3u
from .subscribers import standing

_cache: dict[str, tuple[str, str, list[str]]] = {}
_cache_lock = threading.Lock()


def package_sources(package) -> tuple[list[int] | None, set[str]]:
    """(source ids or None for all, group names or empty for all)."""
    if package is None:
        return None, set()
    ids = [int(i) for i in db.loads(package["source_ids"], []) if str(i).isdigit()]
    groups = {str(g).strip().lower() for g in db.loads(package["groups"], []) if str(g).strip()}
    return ids, groups


def entries_text(package) -> tuple[str, list[str]]:
    """The rendered entries for a package, plus the EPG URLs they came with."""
    version = db.playlist_version()
    key = f"{package['id'] if package is not None else 'all'}"
    with _cache_lock:
        cached = _cache.get(key)
        if cached and cached[0] == version:
            return cached[1], cached[2]

    ids, groups = package_sources(package)
    where = ["s.enabled=1", "c.hidden=0"]
    params: list = []
    if ids is not None:
        if not ids:
            where.append("0")
        else:
            where.append(f"c.source_id IN ({','.join('?' * len(ids))})")
            params.extend(ids)
    rows = db.query(
        "SELECT c.name, c.url, c.duration, c.attrs, c.extras, c.group_name, s.epg_urls FROM channels c "
        f"JOIN sources s ON s.id=c.source_id WHERE {' AND '.join(where)} "
        "ORDER BY s.kind='manual', s.position, s.id, c.position",
        params,
    )
    lines: list[str] = []
    epg: list[str] = []
    for row in rows:
        if groups and row["group_name"].strip().lower() not in groups:
            continue
        for url in db.loads(row["epg_urls"], []):
            if url not in epg:
                epg.append(url)
        lines.extend(
            m3u.render_entry(row["name"], row["url"], row["duration"], db.loads(row["attrs"], {}), db.loads(row["extras"], []))
        )
    text = "\n".join(lines)
    with _cache_lock:
        _cache[key] = (version, text, epg)
        if len(_cache) > 64:
            _cache.pop(next(iter(_cache)))
    return text, epg


def auth_line(settings: dict, token: str) -> str:
    base = (settings.get("public_base_url") or "").rstrip("/")
    if not base:
        return ""
    return m3u.render_auth(
        f"{base}/unloop",
        token,
        poll=int(settings.get("auth_poll") or 300),
        grace=int(settings.get("auth_grace") or 3600),
        on_error=str(settings.get("auth_on_error") or "allow"),
    )


def build(subscriber, settings: dict) -> str:
    package = None
    if subscriber["package_id"]:
        package = db.one("SELECT * FROM packages WHERE id=?", (subscriber["package_id"],))
    body, source_epg = entries_text(package)

    epg_urls = [u.strip() for u in str(settings.get("epg_urls") or "").replace("\n", ",").split(",") if u.strip()]
    for url in source_epg:
        if url not in epg_urls:
            epg_urls.append(url)

    name = settings.get("playlist_name") or settings.get("provider_name") or ""
    lines = [m3u.render_header(name, epg_urls)]
    auth = auth_line(settings, subscriber["token"])
    if auth:
        lines.append(auth)

    state = standing(subscriber, settings)
    if state.state in ("block", "expired") and settings.get("playlist_when_refused", "empty") == "empty":
        lines.append(f"# {state.message}")
        return "\n".join(lines) + "\n"
    if body:
        lines.append(body)
    return "\n".join(lines) + "\n"
