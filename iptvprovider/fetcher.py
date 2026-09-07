"""Fetching an upstream playlist, with limits.

Standard library only. The cap on size and the timeout are what keep a
mistyped URL - or a provider that answers with a video file - from filling
the disk or holding the scheduler thread for an afternoon.
"""

from __future__ import annotations

import gzip
import io
import socket
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass

from . import config
from . import __version__

DEFAULT_USER_AGENT = f"IPTVProviderSystem/{__version__} (+https://github.com/)"


class FetchError(Exception):
    """Anything that stops a fetch, with a message fit for the admin screen."""


@dataclass
class Fetched:
    text: str
    size: int
    content_type: str


def decode(data: bytes, content_type: str = "") -> str:
    charset = ""
    if "charset=" in content_type.lower():
        charset = content_type.lower().split("charset=", 1)[1].split(";")[0].strip(" \"'")
    for encoding in (charset, "utf-8", "utf-8-sig", "cp1252", "latin-1"):
        if not encoding:
            continue
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def fetch(url: str, user_agent: str = "", timeout: int | None = None) -> Fetched:
    url = (url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        raise FetchError("The URL must start with http:// or https://")

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": user_agent.strip() or DEFAULT_USER_AGENT,
            "Accept": "*/*",
            "Accept-Encoding": "identity",
        },
    )
    limit = config.MAX_PLAYLIST_BYTES
    try:
        with urllib.request.urlopen(request, timeout=timeout or config.FETCH_TIMEOUT) as response:
            content_type = response.headers.get("Content-Type", "") or ""
            encoding = (response.headers.get("Content-Encoding", "") or "").lower()
            declared = response.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > limit:
                raise FetchError(
                    f"The file is {int(declared) // (1024 * 1024)} MB; the limit is "
                    f"{limit // (1024 * 1024)} MB (IPTV_MAX_PLAYLIST_MB)."
                )
            buffer = io.BytesIO()
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                buffer.write(chunk)
                if buffer.tell() > limit:
                    raise FetchError(
                        f"The file is larger than the {limit // (1024 * 1024)} MB limit "
                        "(IPTV_MAX_PLAYLIST_MB)."
                    )
            data = buffer.getvalue()
    except FetchError:
        raise
    except urllib.error.HTTPError as error:
        raise FetchError(f"The server answered HTTP {error.code} {error.reason}.") from error
    except urllib.error.URLError as error:
        reason = getattr(error, "reason", error)
        raise FetchError(f"Could not connect: {reason}.") from error
    except socket.timeout as error:
        raise FetchError("The server took too long to answer.") from error
    except (OSError, ValueError) as error:
        raise FetchError(f"Could not fetch: {error}.") from error

    if encoding == "gzip" or data[:2] == b"\x1f\x8b":
        try:
            data = gzip.decompress(data)
        except (OSError, EOFError) as error:
            raise FetchError("The gzip content could not be decompressed.") from error
    elif encoding == "deflate":
        try:
            data = zlib.decompress(data)
        except zlib.error:
            data = zlib.decompress(data, -zlib.MAX_WBITS)

    text = decode(data, content_type)
    return Fetched(text=text, size=len(data), content_type=content_type)


def looks_like_playlist(text: str) -> bool:
    head = text.lstrip("\ufeff \r\n\t")[:4096].upper()
    if head.startswith("#EXTM3U") or "#EXTINF" in head:
        return True
    # A list of bare URLs is a playlist too.
    lines = [line.strip() for line in text.splitlines()[:50] if line.strip()]
    if not lines:
        return False
    urls = sum(1 for line in lines if line.lower().startswith(("http://", "https://", "rtmp://", "rtsp://", "udp://")))
    return urls >= max(1, len(lines) // 2) and not head.startswith("<")
