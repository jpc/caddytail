"""
Static file server backed by Caddy's ``file_server`` handler.

Configured via the ``STATIC_PATH`` environment variable (default: ``.``).

Usage with caddytail::

    STATIC_PATH=./public caddytail run myhost caddytail.fileserver:app

Or installed as a systemd service::

    caddytail install myhost caddytail.fileserver:app --env STATIC_PATH=/srv/files
"""

import os
from pathlib import Path

from caddytail.api import static

STATIC_PATH = Path(os.environ.get("STATIC_PATH", ".")).resolve()


def app(environ, start_response):
    """WSGI stub — all file serving is handled by Caddy."""
    start_response("404 Not Found", [("Content-Type", "text/plain")])
    return [b"404 Not Found"]


# Register the static path so Caddy serves files directly with browse enabled.
static(app, "/*", str(STATIC_PATH), browse=True)
