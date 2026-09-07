"""Reading and writing M3U playlists without losing anything on the way.

The parser keeps every directive that belongs to an entry - ``#EXTVLCOPT``,
``#KODIPROP``, ``#EXTHTTP`` and anything else shaped like one - verbatim, so
a playlist that goes in with DRM keys and per-channel headers comes out with
them intact. Attribute names keep their original spelling and order, because
some players are picky about both.

The one line that is never imported is ``#UNLOOP-AUTH``: an upstream
provider's authorisation directive must not leak into the playlists this
panel hands to its own subscribers. The panel writes its own.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

_ATTR_RE = re.compile(
    r"""\s*([A-Za-z0-9_.\-]+)\s*=\s*("([^"]*)"|'([^']*)'|([^\s,]*))"""
)
_EPG_KEYS = ("url-tvg", "x-tvg-url", "tvg-url")


@dataclass
class Entry:
    name: str
    url: str
    duration: str = "-1"
    attrs: dict[str, str] = field(default_factory=dict)
    extras: list[str] = field(default_factory=list)
    match_key: str = ""

    def attr(self, key: str, default: str = "") -> str:
        lower = key.lower()
        for name, value in self.attrs.items():
            if name.lower() == lower:
                return value
        return default

    @property
    def group(self) -> str:
        return self.attr("group-title")

    @property
    def tvg_id(self) -> str:
        return self.attr("tvg-id")

    @property
    def tvg_logo(self) -> str:
        return self.attr("tvg-logo")


@dataclass
class Playlist:
    entries: list[Entry] = field(default_factory=list)
    header_attrs: dict[str, str] = field(default_factory=dict)
    epg_urls: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def parse_attrs(text: str) -> tuple[dict[str, str], int]:
    """Reads ``key="value"`` pairs from the start of *text*.

    Returns the attributes and the offset just past the last one, so the
    caller can find the comma that ends the attribute region. Quote-aware:
    a comma inside quotes never ends anything.
    """
    attrs: dict[str, str] = {}
    pos = 0
    while True:
        match = _ATTR_RE.match(text, pos)
        if not match:
            break
        key = match.group(1)
        value = match.group(3)
        if value is None:
            value = match.group(4)
        if value is None:
            value = match.group(5) or ""
        attrs[key] = _unescape(value)
        pos = match.end()
    return attrs, pos


def _unescape(value: str) -> str:
    if "&" in value:
        value = (
            value.replace("&amp;", "&")
            .replace("&quot;", '"')
            .replace("&#39;", "'")
            .replace("&lt;", "<")
            .replace("&gt;", ">")
        )
    return value.strip()


def parse_extinf(line: str) -> tuple[str, dict[str, str], str]:
    """``#EXTINF:-1 tvg-id="x",Name`` -> (duration, attrs, name)."""
    body = line[len("#EXTINF:"):]
    duration_match = re.match(r"\s*([-+]?\d+(?:\.\d+)?)", body)
    if duration_match:
        duration = duration_match.group(1)
        rest = body[duration_match.end():]
    else:
        duration = "-1"
        rest = body
    attrs, end = parse_attrs(rest)
    tail = rest[end:]
    comma = tail.find(",")
    name = tail[comma + 1:].strip() if comma >= 0 else tail.strip()
    return duration, attrs, name


def _is_directive(line: str) -> bool:
    """A ``#`` line worth keeping with the next URL, as opposed to a comment."""
    if len(line) < 2:
        return False
    return line[1].isalpha()


def parse(text: str) -> Playlist:
    playlist = Playlist()
    text = text.lstrip("\ufeff")

    pending_extinf: tuple[str, dict[str, str], str] | None = None
    pending_extras: list[str] = []
    pending_group = ""
    unnamed = 0
    skipped_auth = 0

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue

        if line.startswith("#"):
            upper = line.upper()
            if upper.startswith("#EXTM3U"):
                attrs, _ = parse_attrs(line[len("#EXTM3U"):])
                for key, value in attrs.items():
                    if key.lower() in _EPG_KEYS:
                        for url in value.split(","):
                            url = url.strip()
                            if url and url not in playlist.epg_urls:
                                playlist.epg_urls.append(url)
                    else:
                        playlist.header_attrs[key] = value
                continue
            if upper.startswith("#EXTINF:"):
                pending_extinf = parse_extinf(line)
                continue
            if upper.startswith("#EXTGRP:"):
                pending_group = line[len("#EXTGRP:"):].strip()
                continue
            if upper.startswith("#UNLOOP-AUTH"):
                skipped_auth += 1
                continue
            if upper.startswith("#EXT-X-ENDLIST") or upper.startswith("#EXT-X-VERSION"):
                continue
            if _is_directive(line):
                pending_extras.append(line)
            continue

        # A URL line closes the entry.
        if pending_extinf is None:
            unnamed += 1
            duration, attrs, name = "-1", {}, f"Channel {unnamed}"
        else:
            duration, attrs, name = pending_extinf
            if not name:
                unnamed += 1
                name = f"Channel {unnamed}"
        if pending_group and not any(k.lower() == "group-title" for k in attrs):
            attrs["group-title"] = pending_group
        playlist.entries.append(
            Entry(name=name, url=line, duration=duration, attrs=attrs, extras=pending_extras)
        )
        pending_extinf = None
        pending_extras = []
        pending_group = ""  # #EXTGRP names the next entry only, as the app reads it

    if skipped_auth:
        playlist.warnings.append(
            f"Dropped {skipped_auth} upstream #UNLOOP-AUTH line(s); this panel writes its own."
        )
    assign_match_keys(playlist.entries)
    return playlist


def assign_match_keys(entries: list[Entry]) -> None:
    """Gives every entry a key that survives a refresh of the same source.

    Preference order: a unique ``tvg-id``, then a unique group+name, then the
    URL. Duplicates get an occurrence suffix so that two entries never share
    a key - an override must land on exactly one channel.
    """
    ids = Counter(e.tvg_id.strip().lower() for e in entries if e.tvg_id.strip())
    names = Counter((e.group.strip().lower(), e.name.strip().lower()) for e in entries)
    seen: Counter[str] = Counter()
    for entry in entries:
        tid = entry.tvg_id.strip().lower()
        if tid and ids[tid] == 1:
            base = f"id:{tid}"
        elif names[(entry.group.strip().lower(), entry.name.strip().lower())] == 1:
            base = f"n:{entry.group.strip().lower()}|{entry.name.strip().lower()}"
        else:
            base = f"u:{entry.url.strip()}"
        seen[base] += 1
        entry.match_key = base if seen[base] == 1 else f"{base}#{seen[base]}"


# ------------------------------------------------------------------ writing


def set_attr(attrs: dict[str, str], key: str, value: str) -> None:
    """Sets or removes an attribute, keeping the original spelling of the key."""
    lower = key.lower()
    for existing in list(attrs):
        if existing.lower() == lower:
            if value:
                attrs[existing] = value
            else:
                del attrs[existing]
            return
    if value:
        attrs[key] = value


def quote(value: str) -> str:
    return '"' + value.replace('"', "'").replace("\r", " ").replace("\n", " ") + '"'


def render_attrs(attrs: dict[str, str]) -> str:
    return " ".join(f"{key}={quote(value)}" for key, value in attrs.items() if value != "")


def render_header(name: str = "", epg_urls: list[str] | None = None, extra: dict[str, str] | None = None) -> str:
    attrs: dict[str, str] = {}
    if name:
        attrs["name"] = name
    if extra:
        for key, value in extra.items():
            if key.lower() in _EPG_KEYS or key.lower() == "name":
                continue
            attrs[key] = value
    if epg_urls:
        attrs["url-tvg"] = ",".join(u.strip() for u in epg_urls if u.strip())
    line = "#EXTM3U"
    rendered = render_attrs(attrs)
    return f"{line} {rendered}" if rendered else line


def render_auth(url: str, token: str, poll: int = 300, grace: int = 3600, on_error: str = "allow") -> str:
    parts = [f'url={quote(url)}']
    if token:
        parts.append(f"token={quote(token)}")
    parts.append(f'poll="{int(poll)}"')
    parts.append(f'grace="{int(grace)}"')
    parts.append(f'on-error="{"deny" if on_error == "deny" else "allow"}"')
    return "#UNLOOP-AUTH:1 " + " ".join(parts)


def render_entry(name: str, url: str, duration: str, attrs: dict[str, str], extras: list[str]) -> list[str]:
    lines = [line for line in extras if line.strip() and not line.upper().startswith("#UNLOOP-AUTH")]
    rendered = render_attrs(attrs)
    head = f"#EXTINF:{duration or '-1'}"
    if rendered:
        head += " " + rendered
    safe_name = name.replace("\r", " ").replace("\n", " ")
    lines.append(f"{head},{safe_name}")
    lines.append(url.replace("\r", "").replace("\n", ""))
    return lines
