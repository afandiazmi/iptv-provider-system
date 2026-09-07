"""The background thread that refreshes URL sources on their own schedule
and keeps the activity log from growing without bound."""

from __future__ import annotations

import logging
import threading

from . import config, db, library

log = logging.getLogger("iptvprovider.scheduler")

_thread: threading.Thread | None = None
_stop = threading.Event()


def tick() -> None:
    for source in library.sources_due():
        ok, message = library.refresh_source(source["id"])
        (log.info if ok else log.warning)("refresh %s (%s): %s", source["name"], source["id"], message)

    keep = config.ACTIVITY_KEEP_ROWS
    total = db.scalar("SELECT COUNT(*) FROM activity") or 0
    if total > keep * 1.1:
        cutoff = db.scalar("SELECT id FROM activity ORDER BY id DESC LIMIT 1 OFFSET ?", (keep,))
        if cutoff:
            db.execute("DELETE FROM activity WHERE id <= ?", (cutoff,))


def _run() -> None:
    # Let the web server come up first.
    _stop.wait(5)
    while not _stop.is_set():
        try:
            tick()
        except Exception:  # noqa: BLE001 - the loop must survive anything
            log.exception("scheduler tick failed")
        _stop.wait(config.SCHEDULER_TICK_SECONDS)


def start() -> None:
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_run, name="iptv-scheduler", daemon=True)
    _thread.start()


def stop() -> None:
    _stop.set()
    if _thread is not None:
        _thread.join(timeout=2)
