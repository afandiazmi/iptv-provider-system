"""IPTV Provider System - a self-contained panel for playlist providers.

One Python process serves the admin panel, the per-subscriber playlists and
the ``#UNLOOP-AUTH`` authorisation endpoint that the IPTV Room app calls.
Everything is stored in a single SQLite file; there is nothing else to run.
"""

__version__ = "1.0.0"
