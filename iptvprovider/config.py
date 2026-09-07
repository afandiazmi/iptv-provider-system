"""Process-level configuration, read from the environment with working defaults.

Everything that changes per deployment lives here; everything an admin may
want to change while the panel is running lives in the ``settings`` table
instead (see ``db.py``), so that nothing needs a restart.
"""

from __future__ import annotations

import os
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


BASE_DIR = Path(__file__).resolve().parent.parent

# Where the SQLite database, the secret key and uploaded files live.
DATA_DIR = Path(os.environ.get("IPTV_DATA_DIR", BASE_DIR / "data")).resolve()

# The port the IPTV Room documentation example uses. Change with IPTV_PORT.
HOST = os.environ.get("IPTV_HOST", "0.0.0.0")
PORT = _env_int("IPTV_PORT", 8302)

# The largest playlist this will read, from an upload or a URL, in bytes.
MAX_PLAYLIST_BYTES = _env_int("IPTV_MAX_PLAYLIST_MB", 64) * 1024 * 1024

# How long one fetch of an upstream playlist may take, in seconds.
FETCH_TIMEOUT = _env_int("IPTV_FETCH_TIMEOUT", 60)

# How often the background scheduler looks for sources due a refresh.
SCHEDULER_TICK_SECONDS = _env_int("IPTV_SCHEDULER_TICK", 60)

# Rows kept in the activity log. Older rows are pruned by the scheduler.
ACTIVITY_KEEP_ROWS = _env_int("IPTV_ACTIVITY_KEEP_ROWS", 20000)

# Number of rows per page in the admin panel.
PAGE_SIZE = _env_int("IPTV_PAGE_SIZE", 50)

# Set to "1" to run Flask's debug server instead of waitress.
DEBUG = os.environ.get("IPTV_DEBUG", "").strip() in {"1", "true", "yes"}
