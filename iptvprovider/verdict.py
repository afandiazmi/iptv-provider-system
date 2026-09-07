"""The answer to ``POST /unloop`` - the one endpoint the IPTV Room app calls.

Follows https://iptvroom.nax.my/docs#authorisation exactly: the app sends a
JSON body naming the device and the subscriber's token; this replies with
one of four states and, optionally, a message, a QR-code URL, a device
count, a re-check interval and stream headers.

Anything other than ``200`` with valid JSON is read by the app as "could
not be reached", not as a refusal - so every path here, including the ones
that refuse, returns a proper ``200`` body.
"""

from __future__ import annotations

import re
from typing import Any

from . import db, subscribers

_DEVICE_ID = re.compile(r"^[A-Za-z0-9._:-]{4,64}$")
# Header names the app refuses outright, so there is no point sending them.
_REFUSED_HEADERS = {"host", "content-length", "connection", "transfer-encoding", "upgrade", "expect", "te", "trailer"}


def _clean(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"[\x00-\x1f\x7f]", "", value).strip()[:limit]


def _stream_block(settings: dict) -> dict | None:
    raw = settings.get("stream_headers") or ""
    headers = db.loads(raw, None) if raw.strip() else None
    if not isinstance(headers, dict) or not headers:
        return None
    clean = {}
    for key, value in list(headers.items())[:8]:
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9-]{0,63}$", key) or key.lower() in _REFUSED_HEADERS:
            continue
        clean[key] = re.sub(r"[\x00-\x1f\x7f]", "", value)[:2048]
    if not clean:
        return None
    ttl = int(settings.get("stream_headers_ttl") or 600)
    return {"headers": clean, "expires_in": max(10, min(86400, ttl))}


def answer(payload: dict | None, header_device_id: str = "", ip: str = "") -> dict:
    """Builds the JSON reply and records the call. Never raises."""
    settings = db.get_settings()
    payload = payload if isinstance(payload, dict) else {}

    token = _clean(payload.get("token") or payload.get("account"), 512)
    device_id = _clean(payload.get("device_id") or header_device_id, 64)
    if not _DEVICE_ID.match(device_id):
        device_id = ""
    session_id = _clean(payload.get("session_id"), 64)
    reason = _clean(payload.get("reason"), 16) or "poll"
    os_name = _clean(payload.get("os"), 80)
    app_version = _clean(payload.get("app_version"), 40)
    stable = payload.get("device_stable")
    stable = bool(stable) if isinstance(stable, bool) else True

    recheck = int(settings.get("auth_poll") or 300)
    reply: dict[str, Any] = {"v": 1, "state": "ok", "recheck": max(30, min(21600, recheck))}
    stream = _stream_block(settings)

    def log(subscriber_id: int | None, state: str, detail: str = "") -> None:
        try:
            db.execute(
                "INSERT INTO activity(at, subscriber_id, token, device_id, session_id, reason, state, detail, ip, os, app_version) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (db.now_iso(), subscriber_id, token[:64], device_id, session_id, reason, state, detail[:200], ip[:64], os_name, app_version),
            )
        except Exception:  # noqa: BLE001 - logging must never break the answer
            pass

    subscriber = subscribers.by_token(token) if token else None
    if subscriber is None:
        reply.update(
            state="block",
            message=settings["msg_not_found"],
        )
        support = (settings.get("support_url") or settings.get("renew_url") or "").strip()
        if support.startswith("https://"):
            reply["action_url"] = support
            reply["action_label"] = "Scan for help"
        log(None, "block", "unknown token")
        return reply

    state = subscribers.standing(subscriber, settings)
    limit = max(1, int(subscriber["max_devices"]))
    now = db.now_iso()

    if state.state in ("block", "expired"):
        reply.update(state=state.state, message=state.message)
        if state.action_url.startswith("https://"):
            reply["action_url"] = state.action_url
            if state.action_label:
                reply["action_label"] = state.action_label
        known = db.one("SELECT slot FROM devices WHERE subscriber_id=? AND device_id=?", (subscriber["id"], device_id)) if device_id else None
        reply["device"] = {"registered": bool(known), "limit": limit}
        if known:
            reply["device"]["slot"] = int(known["slot"])
        db.execute("UPDATE subscribers SET last_seen_at=? WHERE id=?", (now, subscriber["id"]))
        log(subscriber["id"], state.state, state.reason)
        return reply

    slot = None
    if device_id:
        with db.transaction() as conn:
            slot = subscribers.claim_slot(conn, subscriber, device_id, os_name, app_version, stable)
            conn.execute("UPDATE subscribers SET last_seen_at=? WHERE id=?", (now, subscriber["id"]))
        if slot is None:
            devices_url = (subscriber["action_url"] or settings.get("support_url") or subscribers.self_service_url(settings, subscriber["token"]) or "").strip()
            reply.update(
                state="block",
                message=subscribers.fill(settings["msg_device_limit"], limit=limit, name=subscriber["name"], days=state.days_left or 0, date=""),
                device={"registered": False, "limit": limit},
            )
            if devices_url.startswith("https://"):
                reply["action_url"] = devices_url
                reply["action_label"] = settings.get("action_label_devices", "")[:40]
            log(subscriber["id"], "block", "device limit reached")
            return reply
    else:
        db.execute("UPDATE subscribers SET last_seen_at=? WHERE id=?", (now, subscriber["id"]))

    reply["state"] = state.state
    if state.message:
        reply["message"] = state.message
    if state.action_url.startswith("https://"):
        reply["action_url"] = state.action_url
        if state.action_label:
            reply["action_label"] = state.action_label[:40]
    reply["device"] = {"registered": slot is not None, "limit": limit}
    if slot is not None:
        reply["device"]["slot"] = slot
    reply["channels"] = {"blocked": []}
    if stream:
        reply["stream"] = stream
    log(subscriber["id"], state.state, state.reason)
    return reply
