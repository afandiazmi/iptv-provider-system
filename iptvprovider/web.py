"""The Flask application: admin panel, subscriber playlists, ``/unloop``."""

from __future__ import annotations

import json
import logging
import math
import secrets
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlencode

from flask import (
    Flask,
    Response,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

from . import __version__, auth, config, db, fetcher, library, playlist, scheduler, subscribers, verdict

log = logging.getLogger("iptvprovider.web")

PUBLIC_PREFIXES = ("/p/", "/playlist/", "/me/", "/static/", "/healthz", "/favicon.ico")


class Pager:
    def __init__(self, page: int, total: int, per_page: int | None = None):
        self.per_page = max(5, min(500, per_page or config.PAGE_SIZE))
        self.total = int(total)
        self.pages = max(1, math.ceil(self.total / self.per_page))
        self.page = max(1, min(page, self.pages))
        self.offset = (self.page - 1) * self.per_page

    @property
    def has_prev(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.pages

    @property
    def start(self) -> int:
        return 0 if self.total == 0 else self.offset + 1

    @property
    def end(self) -> int:
        return min(self.total, self.offset + self.per_page)

    def window(self) -> list[int | None]:
        """Page numbers to draw, with None for a gap."""
        if self.pages <= 9:
            return list(range(1, self.pages + 1))
        keep = {1, 2, self.pages - 1, self.pages, self.page - 1, self.page, self.page + 1}
        out: list[int | None] = []
        for number in range(1, self.pages + 1):
            if number in keep:
                out.append(number)
            elif out and out[-1] is not None:
                out.append(None)
        return out


def _int(value, default: int = 0, low: int | None = None, high: int | None = None) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        number = default
    if low is not None:
        number = max(low, number)
    if high is not None:
        number = min(high, number)
    return number


def _float(value, default: float = 0.0) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _ids(values) -> list[int]:
    out = []
    for value in values or []:
        for part in str(value).split(","):
            part = part.strip()
            if part.isdigit():
                out.append(int(part))
    return out


def create_app() -> Flask:
    db.init()
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).parent / "templates"),
        static_folder=str(Path(__file__).parent / "static"),
    )
    app.config.update(
        SECRET_KEY=db.secret_key(),
        MAX_CONTENT_LENGTH=config.MAX_PLAYLIST_BYTES + 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(days=14),
        JSON_SORT_KEYS=False,
        TEMPLATES_AUTO_RELOAD=config.DEBUG,
    )

    # --------------------------------------------------------------- guards

    @app.before_request
    def gate():
        path = request.path
        g.settings = db.get_settings()
        g.admin = None
        if path == "/unloop" or path.startswith(PUBLIC_PREFIXES):
            return None
        admin_id = session.get("admin_id")
        if admin_id:
            g.admin = db.one("SELECT id, username FROM admins WHERE id=?", (admin_id,))
        if path in ("/login", "/setup"):
            return None
        if g.admin is None:
            if not auth.has_admins():
                return redirect(url_for("setup"))
            session["next"] = request.full_path.rstrip("?") if request.method == "GET" else "/"
            return redirect(url_for("login"))
        if request.method == "POST":
            sent = request.headers.get("X-CSRF") or request.form.get("_csrf") or (request.get_json(silent=True) or {}).get("_csrf")
            if not sent or not secrets.compare_digest(str(sent), csrf_token()):
                if request.is_json or request.headers.get("X-Requested-With"):
                    return jsonify(ok=False, error="Session expired - reload the page and try again."), 400
                flash("Session expired - please try again.", "error")
                return redirect(request.referrer or url_for("dashboard"))
        return None

    @app.after_request
    def headers(response):
        if not request.path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

    def csrf_token() -> str:
        token = session.get("csrf")
        if not token:
            token = secrets.token_urlsafe(24)
            session["csrf"] = token
        return token

    def base_url() -> str:
        configured = (g.settings.get("public_base_url") or "").rstrip("/")
        return configured or request.host_url.rstrip("/")

    @app.context_processor
    def inject():
        return {
            "csrf_token": csrf_token,
            "settings": g.get("settings", {}),
            "admin": g.get("admin"),
            "base_url": base_url,
            "version": __version__,
            "qs": lambda **changes: _qs(changes),
        }

    def _qs(changes: dict) -> str:
        args = {k: v for k, v in request.args.items() if v != ""}
        for key, value in changes.items():
            if value is None or value == "":
                args.pop(key, None)
            else:
                args[key] = value
        return "?" + urlencode(args) if args else ""

    def wants_json() -> bool:
        return request.is_json or request.headers.get("X-Requested-With") == "fetch"

    def done(message: str, category: str = "ok", to: str | None = None, **extra):
        if wants_json():
            return jsonify(ok=category != "error", message=message, **extra)
        flash(message, category)
        return redirect(to or request.referrer or url_for("dashboard"))

    # ------------------------------------------------------------- public

    @app.get("/healthz")
    def healthz():
        return jsonify(ok=True, version=__version__)

    @app.get("/favicon.ico")
    def favicon():
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
            '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
            '<stop offset="0" stop-color="#6d5bf6"/><stop offset="1" stop-color="#5ee7f5"/></linearGradient></defs>'
            '<rect width="32" height="32" rx="8" fill="url(#g)"/></svg>'
        )
        response = Response(svg, mimetype="image/svg+xml")
        response.headers["Cache-Control"] = "public, max-age=86400"
        return response

    @app.route("/unloop", methods=["POST", "GET"])
    def unloop():
        if request.method == "GET":
            return jsonify(v=1, info="POST application/json here - see https://iptvroom.nax.my/docs#authorisation")
        payload = request.get_json(force=True, silent=True)
        if payload is None and request.data:
            # Wrong or missing Content-Type but a JSON body all the same.
            try:
                payload = json.loads(request.data.decode("utf-8", errors="replace"))
            except ValueError:
                payload = None
        ip = (request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote_addr or "")
        reply = verdict.answer(payload, request.headers.get("X-Device-Id", ""), ip)
        response = jsonify(reply)
        response.headers["Content-Type"] = "application/json; charset=utf-8"
        return response

    def _serve_playlist(token: str):
        token = token.strip()
        if token.endswith(".m3u8"):
            token = token[:-5]
        elif token.endswith(".m3u"):
            token = token[:-4]
        subscriber = subscribers.by_token(token)
        if subscriber is None:
            return Response("#EXTM3U\n# Unknown playlist\n", status=404, mimetype="audio/x-mpegurl")
        body = playlist.build(subscriber, g.settings)
        response = Response(body, mimetype="audio/x-mpegurl")
        response.headers["Content-Type"] = "audio/x-mpegurl; charset=utf-8"
        response.headers["Content-Disposition"] = 'inline; filename="playlist.m3u"'
        return response

    @app.get("/p/<token>")
    def playlist_short(token):
        return _serve_playlist(token)

    @app.get("/playlist/<token>")
    def playlist_long(token):
        return _serve_playlist(token)

    @app.route("/me/<token>", methods=["GET", "POST"])
    def self_service(token):
        if not g.settings.get("self_service_enabled", True):
            abort(404)
        subscriber = subscribers.by_token(token.strip())
        if subscriber is None:
            abort(404)
        message = ""
        if request.method == "POST":
            if not g.settings.get("self_service_devices", True):
                abort(403)
            device_id = (request.form.get("device_id") or "").strip()
            if device_id and subscribers.remove_device(subscriber["id"], device_id):
                message = "That device has been removed. It can sign in again the next time it opens the playlist."
        state = subscribers.standing(subscriber, g.settings)
        return render_template(
            "self_service.html",
            subscriber=subscriber,
            state=state,
            devices=subscribers.devices_for(subscriber["id"]),
            message=message,
            renew_url=(g.settings.get("renew_url") or "").strip(),
            can_remove=bool(g.settings.get("self_service_devices", True)),
        )

    # ------------------------------------------------------------ session

    @app.route("/setup", methods=["GET", "POST"])
    def setup():
        if auth.has_admins():
            return redirect(url_for("login"))
        error = ""
        if request.method == "POST":
            username = (request.form.get("username") or "").strip()
            password = request.form.get("password") or ""
            confirm = request.form.get("confirm") or ""
            base = (request.form.get("public_base_url") or "").strip().rstrip("/")
            if len(username) < 3:
                error = "The username needs at least 3 characters."
            elif len(password) < 8:
                error = "The password needs at least 8 characters."
            elif password != confirm:
                error = "The two passwords do not match."
            else:
                admin_id = auth.create_admin(username, password)
                values = {"public_base_url": base}
                name = (request.form.get("provider_name") or "").strip()
                if name:
                    values["provider_name"] = name
                db.set_settings(values)
                session.clear()
                session["admin_id"] = admin_id
                session.permanent = True
                flash("Welcome. Add your first playlist source to get started.", "ok")
                return redirect(url_for("sources_page"))
        return render_template("setup.html", error=error, guessed_base=request.host_url.rstrip("/"))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if not auth.has_admins():
            return redirect(url_for("setup"))
        if g.get("admin"):
            return redirect(url_for("dashboard"))
        error = ""
        if request.method == "POST":
            key = request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote_addr or "?"
            if auth.too_many_attempts(key):
                error = "Too many attempts. Wait ten minutes and try again."
            else:
                admin = auth.authenticate(request.form.get("username") or "", request.form.get("password") or "")
                if admin is None:
                    auth.record_attempt(key)
                    error = "Wrong username or password."
                else:
                    auth.clear_attempts(key)
                    target = session.pop("next", None) or url_for("dashboard")
                    session.clear()
                    session["admin_id"] = admin["id"]
                    session.permanent = True
                    if not target.startswith("/") or target.startswith("//"):
                        target = url_for("dashboard")
                    return redirect(target)
        return render_template("login.html", error=error)

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    # ---------------------------------------------------------- dashboard

    @app.get("/")
    def dashboard():
        lib = library.stats()
        subs = subscribers.stats(g.settings)
        recent = db.query(
            "SELECT a.*, s.name AS subscriber_name FROM activity a LEFT JOIN subscribers s ON s.id=a.subscriber_id "
            "ORDER BY a.id DESC LIMIT 12"
        )
        sources = library.list_sources()
        expiring = db.query(
            "SELECT * FROM subscribers WHERE status != 'blocked' AND expires_at IS NOT NULL AND expires_at > ? ORDER BY expires_at LIMIT 8",
            (db.now_iso(),),
        )
        return render_template(
            "dashboard.html",
            lib=lib,
            subs=subs,
            recent=recent,
            sources=sources,
            expiring=[(row, subscribers.standing(row, g.settings)) for row in expiring],
            https_ok=base_url().startswith("https://"),
            base_set=bool(g.settings.get("public_base_url")),
        )

    # ------------------------------------------------------------ sources

    @app.get("/sources")
    def sources_page():
        rows = library.list_sources()
        group_counts = {
            r["source_id"]: r["n"]
            for r in db.query("SELECT source_id, COUNT(DISTINCT group_name) AS n FROM channels WHERE hidden=0 GROUP BY source_id")
        }
        return render_template("sources.html", sources=rows, manual=library.manual_source(), group_counts=group_counts)

    def _import_upload(source_id: int, file_storage) -> tuple[bool, str]:
        data = file_storage.read(config.MAX_PLAYLIST_BYTES + 1)
        if len(data) > config.MAX_PLAYLIST_BYTES:
            return False, f"The file is larger than the {config.MAX_PLAYLIST_BYTES // (1024 * 1024)} MB limit."
        text = fetcher.decode(data)
        if not fetcher.looks_like_playlist(text):
            return False, "That file does not look like an M3U playlist."
        result = library.import_text(source_id, text)
        note = f"{result.channels} channels in {result.groups} groups imported."
        if result.warnings:
            note += " " + " ".join(result.warnings)
        db.execute("UPDATE sources SET last_bytes=?, last_error='' WHERE id=?", (len(data), source_id))
        return True, note

    @app.post("/sources/add")
    def sources_add():
        kind = (request.form.get("kind") or "url").strip()
        name = (request.form.get("name") or "").strip()
        user_agent = (request.form.get("user_agent") or "").strip()
        hours = _float(request.form.get("auto_refresh_hours"), 0)
        if kind == "url":
            url = (request.form.get("url") or "").strip()
            if not url.lower().startswith(("http://", "https://")):
                return done("Enter a playlist URL that starts with http:// or https://.", "error")
            existing = library.find_source_by_url(url)
            if existing:
                # Same link again: replace, never duplicate.
                library.update_source(existing["id"], name=name or existing["name"], user_agent=user_agent, auto_refresh_hours=hours)
                ok, note = library.refresh_source(existing["id"])
                return done(("Replaced: " if ok else "Could not refresh: ") + note, "ok" if ok else "error", url_for("sources_page"))
            if not name:
                name = url.split("//", 1)[-1].split("/")[0] or "Playlist"
            source_id = library.create_source(name, "url", url=url, user_agent=user_agent, auto_refresh_hours=hours)
            ok, note = library.refresh_source(source_id)
            if not ok:
                return done(f"Added, but the first fetch failed: {note}", "error", url_for("sources_page"))
            return done(f"Added {name}: {note}.", "ok", url_for("channels_page", source=source_id))
        if kind == "upload":
            files = [f for f in request.files.getlist("file") if f and f.filename]
            if not files:
                return done("Choose at least one .m3u file to upload.", "error")
            imported = 0
            problems = []
            replace_id = _int(request.form.get("replace_source_id"), 0)
            for index, file_storage in enumerate(files):
                if replace_id and len(files) == 1:
                    source_id = replace_id
                    if name:
                        library.update_source(source_id, name=name)
                else:
                    label = name if (name and len(files) == 1) else Path(file_storage.filename).stem
                    source_id = library.create_source(label or f"Upload {index + 1}", "upload")
                ok, note = _import_upload(source_id, file_storage)
                if ok:
                    imported += 1
                else:
                    problems.append(f"{file_storage.filename}: {note}")
                    if not replace_id:
                        library.delete_source(source_id)
            if problems:
                return done(f"Imported {imported} file(s). Problems: " + "; ".join(problems), "error", url_for("sources_page"))
            return done(f"Imported {imported} file(s).", "ok", url_for("sources_page"))
        return done("Unknown source type.", "error")

    @app.post("/sources/<int:source_id>/refresh")
    def sources_refresh(source_id):
        ok, note = library.refresh_source(source_id)
        return done(("Refreshed: " if ok else "Refresh failed: ") + note, "ok" if ok else "error", request.referrer or url_for("sources_page"))

    @app.post("/sources/<int:source_id>/update")
    def sources_update(source_id):
        source = library.get_source(source_id)
        if source is None:
            abort(404)
        fields = {
            "name": (request.form.get("name") or source["name"]).strip() or source["name"],
            "user_agent": (request.form.get("user_agent") or "").strip(),
            "auto_refresh_hours": _float(request.form.get("auto_refresh_hours"), 0),
            "enabled": 1 if request.form.get("enabled", "1") in ("1", "on", "true") else 0,
        }
        if source["kind"] == "url":
            url = (request.form.get("url") or source["url"]).strip()
            if url.lower().startswith(("http://", "https://")):
                fields["url"] = url
        library.update_source(source_id, **fields)
        return done("Source saved.", "ok", url_for("sources_page"))

    @app.post("/sources/<int:source_id>/replace")
    def sources_replace(source_id):
        source = library.get_source(source_id)
        if source is None:
            abort(404)
        file_storage = request.files.get("file")
        if not file_storage or not file_storage.filename:
            return done("Choose a file to upload.", "error")
        ok, note = _import_upload(source_id, file_storage)
        if ok and source["kind"] == "manual":
            note += " Manual channels are replaced by the file."
        return done(("Replaced: " if ok else "Could not import: ") + note, "ok" if ok else "error", url_for("channels_page", source=source_id))

    @app.post("/sources/<int:source_id>/reset")
    def sources_reset(source_id):
        ok, note = library.clear_overrides(source_id)
        return done(("Edits cleared. " if ok else "") + note, "ok" if ok else "error", url_for("channels_page", source=source_id))

    @app.post("/sources/<int:source_id>/delete")
    def sources_delete(source_id):
        if library.delete_source(source_id):
            return done("Source deleted.", "ok", url_for("sources_page"))
        return done("The Manual source cannot be deleted - delete its channels instead.", "error")

    @app.get("/sources/<int:source_id>/download")
    def sources_download(source_id):
        source = library.get_source(source_id)
        if source is None:
            abort(404)
        path = library.raw_path(source_id)
        if not path.exists():
            abort(404)
        return send_file(path, mimetype="audio/x-mpegurl", as_attachment=True, download_name=f"{source['name']}.m3u")

    @app.post("/sources/bulk")
    def sources_bulk():
        action = request.form.get("action") or (request.get_json(silent=True) or {}).get("action") or ""
        ids = _ids(request.form.getlist("ids") or (request.get_json(silent=True) or {}).get("ids") or [])
        if not ids:
            return done("Select at least one source.", "error")
        count = 0
        notes = []
        for source_id in ids:
            if action == "delete":
                count += 1 if library.delete_source(source_id) else 0
            elif action == "refresh":
                ok, note = library.refresh_source(source_id)
                count += 1 if ok else 0
                if not ok:
                    notes.append(note)
            elif action in ("enable", "disable"):
                library.update_source(source_id, enabled=1 if action == "enable" else 0)
                count += 1
        text = f"{action.capitalize()}d {count} source(s)." if action != "refresh" else f"Refreshed {count} source(s)."
        if notes:
            text += " " + "; ".join(notes[:3])
        return done(text, "ok" if count else "error", url_for("sources_page"))

    # ----------------------------------------------------------- channels

    @app.get("/channels")
    def channels_page():
        source_id = _int(request.args.get("source"), 0) or None
        group = request.args.get("group")
        search = (request.args.get("q") or "").strip()
        show = request.args.get("show") or "visible"
        page = _int(request.args.get("page"), 1, 1)
        per_page = _int(request.args.get("per"), config.PAGE_SIZE, 10, 500)
        where, params = library.channel_filter(
            source_id, group if group else None, search,
            include_hidden=show == "all", only_hidden=show == "hidden",
        )
        total = db.scalar(f"SELECT COUNT(*) FROM channels c WHERE {where}", params) or 0
        pager = Pager(page, total, per_page)
        rows = db.query(
            f"SELECT c.*, s.name AS source_name, s.kind AS source_kind FROM channels c JOIN sources s ON s.id=c.source_id "
            f"WHERE {where} ORDER BY s.kind='manual', s.position, c.source_id, c.position LIMIT ? OFFSET ?",
            [*params, pager.per_page, pager.offset],
        )
        source = library.get_source(source_id) if source_id else None
        return render_template(
            "channels.html",
            rows=rows,
            pager=pager,
            sources=library.list_sources(),
            source=source,
            groups=library.groups_for(source_id),
            group=group or "",
            search=search,
            show=show,
            manual=library.manual_source(),
        )

    @app.get("/channels/<int:channel_id>.json")
    def channel_json(channel_id):
        row = library.get_channel(channel_id)
        if row is None:
            return jsonify(ok=False, error="No such channel"), 404
        return jsonify(
            ok=True,
            channel={
                "id": row["id"],
                "name": row["name"],
                "url": row["url"],
                "group_name": row["group_name"],
                "tvg_id": row["tvg_id"],
                "tvg_logo": row["tvg_logo"],
                "duration": row["duration"],
                "extras": "\n".join(db.loads(row["extras"], [])),
                "attrs": db.loads(row["attrs"], {}),
                "hidden": bool(row["hidden"]),
                "edited": bool(row["edited"]),
                "source_name": row["source_name"],
                "source_kind": row["source_kind"],
            },
        )

    @app.post("/channels/<int:channel_id>/update")
    def channel_update(channel_id):
        data = request.get_json(silent=True) or request.form
        fields = {key: data.get(key) for key in library.EDITABLE if key in data}
        if not fields:
            return done("Nothing to save.", "error")
        if "url" in fields and not str(fields["url"]).strip():
            return done("The stream URL cannot be empty.", "error")
        if library.update_channel(channel_id, fields):
            return done("Channel saved.")
        return done("No such channel.", "error")

    @app.post("/channels/add")
    def channel_add():
        data = request.get_json(silent=True) or request.form
        if not str(data.get("url", "")).strip():
            return done("A stream URL is required.", "error")
        channel_id = library.add_manual_channel(dict(data))
        return done("Channel added to the Manual source.", "ok", url_for("channels_page", source=library.manual_source()["id"]), id=channel_id)

    @app.post("/channels/bulk")
    def channels_bulk():
        data = request.get_json(silent=True) or {}
        action = data.get("action") or request.form.get("action") or ""
        select_all = bool(data.get("all")) or request.form.get("all") == "1"
        hidden = action in ("hide", "delete")
        if action not in ("hide", "delete", "restore"):
            return done("Unknown action.", "error")
        if select_all:
            filters = data.get("filter") or request.form
            source_id = _int(filters.get("source"), 0) or None
            group = filters.get("group") or None
            search = (filters.get("q") or "").strip()
            changed = library.set_hidden_by_filter(source_id, hidden, group, search)
        else:
            ids = _ids(data.get("ids") or request.form.getlist("ids"))
            changed = library.set_hidden(ids, hidden)
        verb = "Deleted" if hidden else "Restored"
        return done(f"{verb} {changed} channel(s).", "ok", changed=changed)

    # ----------------------------------------------------------- packages

    @app.get("/packages")
    def packages_page():
        rows = db.query("SELECT * FROM packages ORDER BY name")
        counts = {row["id"]: db.scalar("SELECT COUNT(*) FROM subscribers WHERE package_id=?", (row["id"],)) for row in rows}
        return render_template(
            "packages.html",
            packages=[(row, db.loads(row["source_ids"], []), db.loads(row["groups"], []), counts[row["id"]]) for row in rows],
            sources=library.list_sources(),
            all_groups=[r["group_name"] for r in library.groups_for(None) if r["group_name"]],
        )

    def _package_fields():
        name = (request.form.get("name") or "").strip()
        source_ids = _ids(request.form.getlist("source_ids"))
        groups = [g.strip() for g in (request.form.get("groups") or "").replace("\n", ",").split(",") if g.strip()]
        return name, source_ids, groups, (request.form.get("description") or "").strip()

    @app.post("/packages/add")
    def packages_add():
        name, source_ids, groups, description = _package_fields()
        if not name:
            return done("Give the package a name.", "error")
        now = db.now_iso()
        db.execute(
            "INSERT INTO packages(name, description, source_ids, groups, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (name, description, db.dumps(source_ids), db.dumps(groups), now, now),
        )
        return done(f"Package {name} created.")

    @app.post("/packages/<int:package_id>/update")
    def packages_update(package_id):
        name, source_ids, groups, description = _package_fields()
        if not name:
            return done("Give the package a name.", "error")
        db.execute(
            "UPDATE packages SET name=?, description=?, source_ids=?, groups=?, updated_at=? WHERE id=?",
            (name, description, db.dumps(source_ids), db.dumps(groups), db.now_iso(), package_id),
        )
        db.bump_playlist_version()
        return done("Package saved.")

    @app.post("/packages/<int:package_id>/delete")
    def packages_delete(package_id):
        db.execute("DELETE FROM packages WHERE id=?", (package_id,))
        db.bump_playlist_version()
        return done("Package deleted. Subscribers on it now see every source.")

    # -------------------------------------------------------- subscribers

    @app.get("/subscribers")
    def subscribers_page():
        search = (request.args.get("q") or "").strip()
        state = request.args.get("state") or ""
        page = _int(request.args.get("page"), 1, 1)
        per_page = _int(request.args.get("per"), config.PAGE_SIZE, 10, 500)
        where, params = subscribers.list_filter(search, state)
        total = db.scalar(f"SELECT COUNT(*) FROM subscribers s WHERE {where}", params) or 0
        pager = Pager(page, total, per_page)
        rows = db.query(
            f"SELECT s.*, p.name AS package_name, (SELECT COUNT(*) FROM devices d WHERE d.subscriber_id=s.id) AS device_count "
            f"FROM subscribers s LEFT JOIN packages p ON p.id=s.package_id WHERE {where} "
            "ORDER BY s.id DESC LIMIT ? OFFSET ?",
            [*params, pager.per_page, pager.offset],
        )
        return render_template(
            "subscribers.html",
            rows=[(row, subscribers.standing(row, g.settings)) for row in rows],
            pager=pager,
            search=search,
            state=state,
            packages=db.query("SELECT id, name FROM packages ORDER BY name"),
            stats=subscribers.stats(g.settings),
        )

    @app.post("/subscribers/add")
    def subscribers_add():
        name = (request.form.get("name") or "").strip()
        if not name:
            return done("Give the subscriber a name.", "error")
        days_raw = (request.form.get("days") or "").strip()
        days = None if days_raw in ("", "0", "never", "custom") else _int(days_raw, int(g.settings.get("default_days") or 30), 1, 36500)
        expires_field = (request.form.get("expires_at") or "").strip()
        max_devices = _int(request.form.get("max_devices"), int(g.settings.get("default_max_devices") or 2), 1, 100)
        package_id = _int(request.form.get("package_id"), 0) or None
        subscriber_id = subscribers.create(
            name, days, max_devices, package_id,
            note=request.form.get("note") or "", contact=request.form.get("contact") or "",
            token=(request.form.get("token") or "").strip(),
        )
        if expires_field:
            parsed = _parse_date(expires_field)
            subscribers.update(subscriber_id, expires_at=parsed)
        return done(f"{name} added.", "ok", url_for("subscriber_page", subscriber_id=subscriber_id))

    def _parse_date(value: str) -> str | None:
        value = value.strip()
        if not value:
            return None
        dt = db.parse_iso(value if "T" in value else value + "T23:59:59Z")
        return db.iso(dt) if dt else None

    @app.get("/subscribers/<int:subscriber_id>")
    def subscriber_page(subscriber_id):
        row = subscribers.get(subscriber_id)
        if row is None:
            abort(404)
        activity = db.query("SELECT * FROM activity WHERE subscriber_id=? ORDER BY id DESC LIMIT 25", (subscriber_id,))
        base = base_url()
        return render_template(
            "subscriber.html",
            s=row,
            state=subscribers.standing(row, g.settings),
            devices=subscribers.devices_for(subscriber_id),
            activity=activity,
            packages=db.query("SELECT id, name FROM packages ORDER BY name"),
            playlist_url=f"{base}/p/{row['token']}.m3u",
            self_url=f"{base}/me/{row['token']}",
            expires_date=(row["expires_at"] or "")[:10],
        )

    @app.post("/subscribers/<int:subscriber_id>/update")
    def subscriber_update(subscriber_id):
        row = subscribers.get(subscriber_id)
        if row is None:
            abort(404)
        expires = (request.form.get("expires_at") or "").strip()
        fields = {
            "name": (request.form.get("name") or row["name"]).strip() or row["name"],
            "note": (request.form.get("note") or "").strip(),
            "contact": (request.form.get("contact") or "").strip(),
            "max_devices": _int(request.form.get("max_devices"), row["max_devices"], 1, 100),
            "package_id": _int(request.form.get("package_id"), 0) or None,
            "action_url": (request.form.get("action_url") or "").strip(),
            "expires_at": _parse_date(expires) if expires else None,
        }
        subscribers.update(subscriber_id, **fields)
        return done("Subscriber saved.")

    @app.post("/subscribers/<int:subscriber_id>/status")
    def subscriber_status(subscriber_id):
        status = (request.form.get("status") or "active").strip()
        if status not in ("active", "warned", "blocked"):
            return done("Unknown status.", "error")
        subscribers.update(subscriber_id, status=status, message=(request.form.get("message") or "").strip())
        label = {"active": "Active", "warned": "Warning shown", "blocked": "Blocked"}[status]
        return done(f"Status set to {label}.")

    @app.post("/subscribers/<int:subscriber_id>/extend")
    def subscriber_extend(subscriber_id):
        days = _int(request.form.get("days"), 30, 1, 36500)
        new_expiry = subscribers.extend(subscriber_id, days)
        if new_expiry is None:
            abort(404)
        return done(f"Extended by {days} day(s), now until {new_expiry[:10]}.")

    @app.post("/subscribers/<int:subscriber_id>/never")
    def subscriber_never(subscriber_id):
        subscribers.update(subscriber_id, expires_at=None)
        return done("This subscription never expires now.")

    @app.post("/subscribers/<int:subscriber_id>/token")
    def subscriber_token(subscriber_id):
        subscribers.regenerate_token(subscriber_id)
        return done("New playlist link generated. The old link stops working immediately and every device was signed out.")

    @app.post("/subscribers/<int:subscriber_id>/devices/remove")
    def subscriber_device_remove(subscriber_id):
        device_id = (request.form.get("device_id") or "").strip()
        if subscribers.remove_device(subscriber_id, device_id):
            return done("Device removed - its slot is free again.")
        return done("No such device.", "error")

    @app.post("/subscribers/<int:subscriber_id>/delete")
    def subscriber_delete(subscriber_id):
        subscribers.delete([subscriber_id])
        return done("Subscriber deleted.", "ok", url_for("subscribers_page"))

    @app.post("/subscribers/<int:subscriber_id>/test")
    def subscriber_test(subscriber_id):
        """Asks /unloop the way the app would, so the admin can see the answer."""
        row = subscribers.get(subscriber_id)
        if row is None:
            abort(404)
        payload = {
            "v": 1,
            "app": "IPTVRoom",
            "app_version": "test",
            "os": "Admin panel test",
            "device_id": (request.form.get("device_id") or "adm1n0000test0001").strip(),
            "device_stable": True,
            "session_id": secrets.token_hex(8),
            "reason": "playlist",
            "token": row["token"],
        }
        reply = verdict.answer(payload, payload["device_id"], "panel")
        return jsonify(ok=True, request=payload, reply=reply)

    @app.post("/subscribers/bulk")
    def subscribers_bulk():
        data = request.get_json(silent=True) or {}
        action = data.get("action") or request.form.get("action") or ""
        ids = _ids(data.get("ids") or request.form.getlist("ids"))
        if data.get("all") or request.form.get("all") == "1":
            filters = data.get("filter") or request.form
            where, params = subscribers.list_filter((filters.get("q") or "").strip(), filters.get("state") or "")
            ids = [r["id"] for r in db.query(f"SELECT s.id FROM subscribers s WHERE {where}", params)]
        if not ids:
            return done("Select at least one subscriber.", "error")
        if action == "delete":
            count = subscribers.delete(ids)
            return done(f"Deleted {count} subscriber(s).", "ok", url_for("subscribers_page"))
        if action in ("block", "unblock", "warn"):
            status = {"block": "blocked", "unblock": "active", "warn": "warned"}[action]
            message = (data.get("message") or request.form.get("message") or "").strip()
            for subscriber_id in ids:
                subscribers.update(subscriber_id, status=status, message=message if status != "active" else "")
            return done(f"Updated {len(ids)} subscriber(s).", "ok", url_for("subscribers_page"))
        if action == "extend":
            days = _int(data.get("days") or request.form.get("days"), 30, 1, 36500)
            for subscriber_id in ids:
                subscribers.extend(subscriber_id, days)
            return done(f"Extended {len(ids)} subscriber(s) by {days} day(s).", "ok", url_for("subscribers_page"))
        return done("Unknown action.", "error")

    # ------------------------------------------------------------ activity

    @app.get("/activity")
    def activity_page():
        page = _int(request.args.get("page"), 1, 1)
        per_page = _int(request.args.get("per"), config.PAGE_SIZE, 10, 500)
        state = request.args.get("state") or ""
        search = (request.args.get("q") or "").strip()
        where = ["1=1"]
        params: list = []
        if state:
            where.append("a.state=?")
            params.append(state)
        if search:
            like = f"%{search}%"
            where.append("(s.name LIKE ? OR a.device_id LIKE ? OR a.token LIKE ? OR a.ip LIKE ?)")
            params.extend([like, like, like, like])
        clause = " AND ".join(where)
        total = db.scalar(f"SELECT COUNT(*) FROM activity a LEFT JOIN subscribers s ON s.id=a.subscriber_id WHERE {clause}", params) or 0
        pager = Pager(page, total, per_page)
        rows = db.query(
            f"SELECT a.*, s.name AS subscriber_name FROM activity a LEFT JOIN subscribers s ON s.id=a.subscriber_id "
            f"WHERE {clause} ORDER BY a.id DESC LIMIT ? OFFSET ?",
            [*params, pager.per_page, pager.offset],
        )
        return render_template("activity.html", rows=rows, pager=pager, state=state, search=search, config_keep=config.ACTIVITY_KEEP_ROWS)

    # ------------------------------------------------------------ settings

    @app.route("/settings", methods=["GET", "POST"])
    def settings_page():
        if request.method == "POST":
            checkboxes = ("self_service_devices", "self_service_enabled")
            values = {}
            for key, default in db.DEFAULT_SETTINGS.items():
                if key in checkboxes:
                    values[key] = 1 if request.form.get(key) in ("1", "on") else 0
                    continue
                if key not in request.form:
                    continue
                raw = request.form.get(key, "")
                if isinstance(default, int):
                    values[key] = _int(raw, default, 0)
                else:
                    values[key] = raw.strip()
            base = values.get("public_base_url", "")
            if base:
                values["public_base_url"] = base.rstrip("/")
                if not base.lower().startswith(("http://", "https://")):
                    return done("The public URL must start with https:// (or http:// for local testing).", "error")
            if values.get("auth_on_error") not in ("allow", "deny"):
                values["auth_on_error"] = "allow"
            values["auth_poll"] = max(30, min(21600, values.get("auth_poll", 300)))
            values["auth_grace"] = max(0, min(604800, values.get("auth_grace", 3600)))
            stream_headers = values.get("stream_headers", "")
            if stream_headers.strip():
                parsed = db.loads(stream_headers, None)
                if not isinstance(parsed, dict):
                    return done("Stream headers must be a JSON object, e.g. {\"X-Play-Token\": \"abc\"}.", "error")
            db.set_settings(values)
            db.bump_playlist_version()
            return done("Settings saved.", "ok", url_for("settings_page"))
        admins = db.query("SELECT id, username, last_login_at FROM admins ORDER BY id")
        return render_template("settings.html", admins=admins, guessed_base=request.host_url.rstrip("/"))

    @app.post("/settings/password")
    def settings_password():
        current = request.form.get("current") or ""
        new = request.form.get("new") or ""
        confirm = request.form.get("confirm") or ""
        admin = db.one("SELECT * FROM admins WHERE id=?", (g.admin["id"],))
        if not auth.check_password(current, admin["password_hash"]):
            return done("The current password is wrong.", "error")
        if len(new) < 8:
            return done("The new password needs at least 8 characters.", "error")
        if new != confirm:
            return done("The two new passwords do not match.", "error")
        auth.change_password(admin["id"], new)
        return done("Password changed.")

    @app.post("/settings/admins/add")
    def settings_admin_add():
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        if len(username) < 3 or len(password) < 8:
            return done("Username needs 3+ characters and password 8+.", "error")
        if db.one("SELECT id FROM admins WHERE username=?", (username,)):
            return done("That username already exists.", "error")
        auth.create_admin(username, password)
        return done(f"Admin {username} added.")

    @app.post("/settings/admins/<int:admin_id>/delete")
    def settings_admin_delete(admin_id):
        if admin_id == g.admin["id"]:
            return done("You cannot delete the account you are signed in with.", "error")
        db.execute("DELETE FROM admins WHERE id=?", (admin_id,))
        return done("Admin removed.")

    # -------------------------------------------------------------- errors

    @app.errorhandler(404)
    def not_found(_error):
        if request.path.startswith(("/unloop", "/p/", "/playlist/")) or wants_json():
            return jsonify(ok=False, error="Not found"), 404
        return render_template("error.html", code=404, title="Not found", text="There is nothing at this address."), 404

    @app.errorhandler(413)
    def too_large(_error):
        return done(f"That upload is larger than the {config.MAX_PLAYLIST_BYTES // (1024 * 1024)} MB limit.", "error")

    @app.errorhandler(500)
    def server_error(_error):
        log.exception("unhandled error")
        if request.path == "/unloop":
            # Still a 200: the app reads anything else as "unreachable", and a
            # panel bug should never lock a paying customer out.
            return jsonify(v=1, state="ok", recheck=90)
        if wants_json():
            return jsonify(ok=False, error="Something went wrong on the server."), 500
        return render_template("error.html", code=500, title="Something went wrong", text="The error has been written to the server log."), 500

    scheduler.start()
    return app
