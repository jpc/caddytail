"""
Runner for ``caddytail run`` and ``caddytail serve``.

Imports the user's app (if any), reads static() registrations,
auto-allocates ports, and manages the Caddy + app lifecycle.
"""

from __future__ import annotations

import importlib
import sys
from typing import Any

from .api import (
    CaddyTail,
    _exposures_from_flags,
    _find_free_port,
    get_registered_exposures,
    get_registered_statics,
    get_tailnet_from_tailscale,
)
from . import get_binary_path


def _parse_app_ref(app_ref: str) -> tuple[str, str]:
    """Parse 'module:variable' into (module_name, variable_name).

    If no variable is given, defaults to 'app'.
    """
    if ":" in app_ref:
        module_name, var_name = app_ref.split(":", 1)
    else:
        module_name = app_ref
        var_name = "app"
    return module_name, var_name


def _import_app(app_ref: str) -> Any:
    """Import the app object from a module:variable reference."""
    module_name, var_name = _parse_app_ref(app_ref)

    import os
    cwd = os.getcwd()
    if cwd not in sys.path:
        sys.path.insert(0, cwd)

    try:
        module = importlib.import_module(module_name)
    except ImportError as e:
        print(f"Error: could not import module '{module_name}': {e}", file=sys.stderr)
        sys.exit(1)

    try:
        app = getattr(module, var_name)
    except AttributeError:
        print(
            f"Error: module '{module_name}' has no attribute '{var_name}'",
            file=sys.stderr,
        )
        sys.exit(1)

    return app


def run(
    hostname: str,
    app_ref: str,
    *,
    debug: bool = False,
    funnel: bool = False,
    tailnet_listener: bool = False,
    server: str = "dev",
    workers: int = 1,
    threads: int = 8,
) -> None:
    """Run an app with Caddy in the foreground. Ctrl-C kills everything.

    ``funnel`` / ``tailnet_listener`` mirror the ``--funnel`` / ``--tailnet``
    CLI flags. When neither is given, expose() registrations on the app are
    used, falling back to a tailnet-only listener.

    ``server`` is ``"dev"`` (in-process) or ``"gunicorn"`` (a hardened
    subprocess). ``workers`` / ``threads`` tune gunicorn.
    """
    app = _import_app(app_ref)

    # Read static() registrations from the app
    static_paths = get_registered_statics(app)

    # Auto-detect tailnet
    tailnet = get_tailnet_from_tailscale()
    if tailnet is None:
        print(
            "Error: could not auto-detect tailnet. "
            "Ensure Tailscale is running and connected.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Flags win; otherwise fall back to expose() registrations, then default.
    if funnel or tailnet_listener:
        exposures = _exposures_from_flags(funnel, tailnet_listener)
    else:
        exposures = get_registered_exposures(app) or None

    # CaddyTail sets up middleware, config, and manages Caddy lifecycle
    caddy = CaddyTail(
        app,
        hostname,
        tailnet=tailnet,
        caddy_path=get_binary_path(),
        app_port=_find_free_port(),
        caddy_http_port=_find_free_port(),
        caddy_admin_port=_find_free_port(),
        static=static_paths or None,
        exposures=exposures,
        server=server,
        workers=workers,
        threads=threads,
        app_ref=app_ref,
        debug=debug,
    )

    urls = "\n  ".join(f"{app_ref} -> {url}" for _, url in caddy.urls)
    print(f"\n  {urls}\n")

    # CaddyTail.run() handles signals, starts Caddy, runs the app,
    # and blocks until shutdown.
    caddy.run()
