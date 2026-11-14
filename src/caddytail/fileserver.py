"""
Static file server backed by Caddy's ``file_server`` handler.

Configured via the ``STATIC_PATH`` environment variable (default: ``.``).
``STATIC_INDEX`` optionally sets the directory-index filenames as a
comma-separated list (e.g. ``index.html,dashboard.html``); unset keeps
Caddy's default (index.html, index.txt).

Usage with caddytail::

    STATIC_PATH=./public caddytail run myhost caddytail.fileserver:app

Or installed as a systemd service::

    caddytail install myhost caddytail.fileserver:app --env STATIC_PATH=/srv/files
"""

import os
from pathlib import Path

from caddytail.api import static

STATIC_PATH = Path(os.environ.get("STATIC_PATH", ".")).resolve()
STATIC_INDEX = [s.strip() for s in os.environ.get("STATIC_INDEX", "").split(",") if s.strip()]


def app(environ, start_response):
    """WSGI stub — all file serving is handled by Caddy."""
    start_response("404 Not Found", [("Content-Type", "text/plain")])
    return [b"404 Not Found"]


# Register the static path so Caddy serves files directly with browse enabled.
static(app, "/*", str(STATIC_PATH), browse=True, index=STATIC_INDEX or None)
