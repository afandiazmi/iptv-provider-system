#!/usr/bin/env python3
"""Run the IPTV Provider System.

    python app.py

Serves the admin panel, subscriber playlists and the /unloop endpoint on
port 8302 (change with IPTV_PORT). Data lives in ./data/provider.db.
"""

from __future__ import annotations

import logging
import sys

from iptvprovider import __version__, config
from iptvprovider.web import create_app


def main() -> None:
    logging.basicConfig(
        level=logging.DEBUG if config.DEBUG else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    app = create_app()
    print(f"IPTV Provider System {__version__}")
    print(f"  admin panel : http://localhost:{config.PORT}/")
    print(f"  data folder : {config.DATA_DIR}")
    print("  Put this behind HTTPS (Caddy, nginx, Cloudflare Tunnel) before giving out playlist links.")
    sys.stdout.flush()

    if config.DEBUG:
        app.run(host=config.HOST, port=config.PORT, debug=True, use_reloader=False)
        return
    try:
        from waitress import serve
    except ImportError:
        print("waitress is not installed; falling back to Flask's development server (pip install waitress)")
        app.run(host=config.HOST, port=config.PORT, threaded=True)
        return
    serve(app, host=config.HOST, port=config.PORT, threads=8, ident="IPTVProviderSystem")


if __name__ == "__main__":
    main()
