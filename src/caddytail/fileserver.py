"""
Minimal WSGI static file server with directory listings.

Configured via the ``STATIC_PATH`` environment variable (default: ``.``).

Usage with caddytail::

    STATIC_PATH=./public caddytail run myhost caddytail.fileserver:app

Or installed as a systemd service::

    caddytail install myhost caddytail.fileserver:app --env STATIC_PATH=/srv/files
"""

import html
import mimetypes
import os
from pathlib import Path
from urllib.parse import unquote

STATIC_PATH = Path(os.environ.get("STATIC_PATH", ".")).resolve()


def app(environ, start_response):
    """WSGI application that serves files from *STATIC_PATH*."""
    request_path = unquote(environ.get("PATH_INFO", "/"))

    # Resolve against root and prevent traversal
    target = (STATIC_PATH / request_path.lstrip("/")).resolve()
    if not str(target).startswith(str(STATIC_PATH)):
        start_response("403 Forbidden", [("Content-Type", "text/plain")])
        return [b"403 Forbidden"]

    if target.is_dir():
        # Serve index.html if present
        index = target / "index.html"
        if index.is_file():
            return _serve_file(index, start_response)
        return _serve_listing(target, request_path, start_response)

    if target.is_file():
        return _serve_file(target, start_response)

    start_response("404 Not Found", [("Content-Type", "text/plain")])
    return [b"404 Not Found"]


def _serve_file(path, start_response):
    content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    data = path.read_bytes()
    start_response("200 OK", [
        ("Content-Type", content_type),
        ("Content-Length", str(len(data))),
    ])
    return [data]


def _serve_listing(directory, url_path, start_response):
    if not url_path.endswith("/"):
        url_path += "/"

    entries = sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    lines = [
        "<!doctype html>",
        f"<html><head><title>Index of {html.escape(url_path)}</title></head>",
        "<body>",
        f"<h1>Index of {html.escape(url_path)}</h1>",
        "<ul>",
    ]
    if url_path != "/":
        lines.append('<li><a href="../">..</a></li>')
    for entry in entries:
        name = entry.name + ("/" if entry.is_dir() else "")
        lines.append(f'<li><a href="{html.escape(name)}">{html.escape(name)}</a></li>')
    lines += ["</ul>", "</body></html>"]

    body = "\n".join(lines).encode()
    start_response("200 OK", [
        ("Content-Type", "text/html; charset=utf-8"),
        ("Content-Length", str(len(body))),
    ])
    return [body]
