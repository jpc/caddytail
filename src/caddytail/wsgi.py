"""Entrypoint for external app servers (gunicorn/uvicorn).

``caddytail run ... --server gunicorn`` spawns gunicorn pointed at
``caddytail.wsgi:application`` with ``CADDYTAIL_APP=module:variable`` set in
the environment. Because worker processes re-import the app fresh, the
ProxyFix / Tailscale middleware that CaddyTail applies to the in-process dev
server is re-applied here.

Despite the module name, ``application`` may be a WSGI *or* ASGI callable —
gunicorn's uvicorn worker serves the ASGI case.
"""

import os

from .api import apply_middleware
from .runner import _import_app

_app_ref = os.environ.get("CADDYTAIL_APP")
if not _app_ref:
    raise RuntimeError(
        "CADDYTAIL_APP is not set. caddytail.wsgi is meant to be launched by "
        "`caddytail run ... --server gunicorn`, not imported directly."
    )

application = apply_middleware(_import_app(_app_ref))
# Common alias so `caddytail.wsgi:app` also works.
app = application
