"""
Tailscale + Caddy reverse proxy wrapper for WSGI and ASGI applications.

Supports Flask, FastAPI, Django, and any generic WSGI/ASGI callable.

Standalone helpers:
    from caddytail import get_user, static, login_required

Runner-first usage (recommended):
    caddytail run myapp app:app

Programmatic usage:
    from caddytail import CaddyTail
    caddy = CaddyTail(app, "myapp")
    caddy.run()
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Union

from . import get_binary_path

# Type aliases
FlaskApp = Any  # flask.Flask
FastAPIApp = Any  # fastapi.FastAPI
StarletteRequest = Any  # starlette.requests.Request


@dataclass
class StaticPath:
    """Configuration for a static file path served by Caddy."""
    url_path: str  # URL path pattern (e.g., "/static/*", "/assets/*")
    local_path: str  # Local filesystem path
    methods: list[str] = field(default_factory=lambda: ["GET", "HEAD"])
    browse: bool = False  # Enable directory listings


# Ports Tailscale Funnel accepts. Mirrors the check in the caddy-tailscale
# plugin (module.go) so we fail early with a clear error instead of at launch.
FUNNEL_PORTS = (443, 8443, 10000)

_NETWORK_BY_MODE = {
    "tailnet": "tailscale",
    "funnel": "tailscale+funnel",
    "funnel-only": "tailscale+funnel-only",
}


@dataclass
class Exposure:
    """A single Caddy listener exposing the app on the tailnet and/or publicly.

    ``mode``:
        - ``"tailnet"``      private to the tailnet, Tailscale-authenticated,
                             injects identity headers (the default).
        - ``"funnel"``       public internet **and** tailnet, unauthenticated.
        - ``"funnel-only"``  public internet only (rejects in-tailnet conns),
                             unauthenticated.

    Funnel traffic carries no Tailscale identity, so funnel exposures cannot be
    authenticated. To serve the public *and* have an authenticated surface, use
    two exposures (e.g. a ``funnel-only`` and a ``tailnet`` one) — see
    ``CaddyTail(..., funnel=True, tailnet=True)``.
    """
    mode: str = "tailnet"
    port: int = 443
    hostname: Optional[str] = None  # defaults to the node hostname
    auth: Optional[bool] = None  # None => True for tailnet, False for funnel

    def __post_init__(self) -> None:
        if self.mode not in _NETWORK_BY_MODE:
            raise ValueError(
                f"invalid exposure mode {self.mode!r}; "
                f"expected one of {sorted(_NETWORK_BY_MODE)}"
            )
        if self.auth is None:
            self.auth = self.mode == "tailnet"
        if self.is_funnel:
            if self.auth:
                raise ValueError(
                    "funnel traffic is unauthenticated and carries no Tailscale "
                    "identity; use a separate tailnet exposure for an "
                    "authenticated surface (e.g. funnel=True, tailnet=True)"
                )
            if self.port not in FUNNEL_PORTS:
                raise ValueError(
                    f"Tailscale Funnel only supports ports {FUNNEL_PORTS} "
                    f"(got {self.port})"
                )

    @property
    def is_funnel(self) -> bool:
        return self.mode in ("funnel", "funnel-only")

    @property
    def network(self) -> str:
        return _NETWORK_BY_MODE[self.mode]


def _exposures_from_flags(funnel: bool, tailnet: bool) -> list["Exposure"]:
    """Map the funnel/tailnet booleans (and CLI flags) to concrete exposures.

    - neither            -> tailnet only on 443 (the default)
    - funnel only        -> public funnel (+ tailnet) on 443
    - funnel and tailnet -> public funnel-only on 443 + authenticated tailnet on 8443

    When both are requested the funnel listener is ``funnel-only`` so the
    authenticated tailnet listener is the single in-tailnet path, and the two
    share one node on distinct ports (Funnel and a plain tailnet listener
    cannot share a port on the same node).
    """
    if funnel and tailnet:
        return [Exposure("funnel-only", 443), Exposure("tailnet", 8443)]
    if funnel:
        return [Exposure("funnel", 443)]
    return [Exposure("tailnet", 443)]


@dataclass
class TailscaleUser:
    """Authenticated Tailscale user information."""
    name: str
    login: str
    profile_pic: str

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "login": self.login,
            "profile_pic": self.profile_pic,
        }


# ---------------------------------------------------------------------------
# Header constants
# ---------------------------------------------------------------------------

HEADER_USER_NAME = "Tailscale-User-Name"
HEADER_USER_LOGIN = "Tailscale-User-Login"
HEADER_USER_PROFILE_PIC = "Tailscale-User-Profile-Pic"
HEADER_NODE = "Tailscale-Node"           # tagged-node hostname (empty for user nodes)
HEADER_TAGS = "Tailscale-Tags"           # comma-separated tags (empty for user nodes)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _extract_user_from_headers(headers: dict[str, str]) -> Optional[TailscaleUser]:
    """Extract Tailscale user from request headers."""
    headers_lower = {k.lower(): v for k, v in headers.items()}

    name_header = headers_lower.get(HEADER_USER_NAME.lower())
    if not name_header:
        return None

    # Decode the name which may be latin1-encoded UTF-8
    try:
        name = name_header.encode('latin1').decode('utf8')
    except (UnicodeDecodeError, UnicodeEncodeError):
        name = name_header

    return TailscaleUser(
        name=name,
        login=headers_lower.get(HEADER_USER_LOGIN.lower(), ""),
        profile_pic=headers_lower.get(HEADER_USER_PROFILE_PIC.lower(), ""),
    )


def _extract_user_from_environ(environ: dict) -> Optional[TailscaleUser]:
    """Extract Tailscale user from a WSGI environ dict.

    WSGI converts headers like ``Tailscale-User-Name`` to
    ``HTTP_TAILSCALE_USER_NAME``.
    """
    name = environ.get("HTTP_TAILSCALE_USER_NAME")
    if not name:
        return None

    # Decode the name which may be latin1-encoded UTF-8
    try:
        name = name.encode('latin1').decode('utf8')
    except (UnicodeDecodeError, UnicodeEncodeError):
        pass

    return TailscaleUser(
        name=name,
        login=environ.get("HTTP_TAILSCALE_USER_LOGIN", ""),
        profile_pic=environ.get("HTTP_TAILSCALE_USER_PROFILE_PIC", ""),
    )


def _find_free_port() -> int:
    """Find an available port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _detect_framework(app: Any) -> str:
    """Detect whether an app is Flask, FastAPI, or a generic WSGI/ASGI callable."""
    import inspect

    app_module = type(app).__module__
    if "flask" in app_module:
        return "flask"
    elif "fastapi" in app_module or "starlette" in app_module:
        return "fastapi"
    elif hasattr(app, "wsgi_app"):
        return "flask"
    elif hasattr(app, "add_middleware"):
        return "fastapi"
    elif (inspect.iscoroutinefunction(app)
          or inspect.iscoroutinefunction(getattr(app, "__call__", None))):
        return "asgi"
    elif callable(app):
        return "wsgi"
    else:
        raise ValueError(f"Not a callable app: {app_module}.{type(app).__name__}")


def get_tailnet_from_tailscale() -> Optional[str]:
    """
    Get the tailnet name from the running Tailscale daemon.

    Returns the tailnet name (without .ts.net suffix), or None if unavailable.
    """
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            status = json.loads(result.stdout)
            magic_dns = status.get("MagicDNSSuffix", "")
            if magic_dns.endswith(".ts.net"):
                return magic_dns[:-7]
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return None


# ---------------------------------------------------------------------------
# Standalone helpers — the primary API for runner-first usage
# ---------------------------------------------------------------------------

def get_user(request: Any = None) -> Optional[TailscaleUser]:
    """
    Get the authenticated Tailscale user.

    Flask: call with no arguments (uses flask.request automatically).
    FastAPI / Starlette: pass the Request object.
    WSGI: pass the environ dict.

    Returns a TailscaleUser or None if not authenticated.
    """
    if request is not None:
        # WSGI environ dict
        if isinstance(request, dict):
            return _extract_user_from_environ(request)
        # FastAPI / Starlette path
        if hasattr(request, "state") and hasattr(request.state, "tailscale_user"):
            return request.state.tailscale_user
        return _extract_user_from_headers(dict(request.headers))

    # Flask path — import request from the active context
    try:
        from flask import request as flask_request
        return _extract_user_from_headers(dict(flask_request.headers))
    except (ImportError, RuntimeError):
        return None


def get_user_or_error(request: Optional[StarletteRequest] = None) -> TailscaleUser:
    """Like get_user() but raises a 401 error if not authenticated."""
    user = get_user(request)
    if user is not None:
        return user

    if request is not None:
        # FastAPI
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Tailscale authentication required")
    else:
        # Flask
        from flask import abort
        abort(401, "Tailscale authentication required")


def login_required(fn: Optional[Callable] = None, *, request: Any = None) -> Any:
    """
    Dual-use authentication guard.

    Flask — use as a decorator:
        @app.get("/secret")
        @login_required
        def secret():
            user = get_user()
            ...

    FastAPI — use as a Depends target:
        @app.get("/secret")
        async def secret(user = Depends(login_required)):
            ...
    """
    # FastAPI Depends path: called with a Request keyword arg by the framework
    if fn is None and request is not None:
        return get_user_or_error(request)

    # When used as Depends(login_required) FastAPI inspects the signature
    # and injects `request`. We need an inner function for that.
    if fn is None:
        # Bare @login_required() call — return self as decorator
        from functools import wraps

        def decorator(f: Callable) -> Callable:
            @wraps(f)
            def wrapper(*args, **kwargs):
                get_user_or_error()
                return f(*args, **kwargs)
            return wrapper
        return decorator

    # Check if fn is actually a Starlette Request (Depends path)
    if hasattr(fn, "headers") and hasattr(fn, "url"):
        return get_user_or_error(fn)

    # Used as @login_required (no parens) on a Flask route
    from functools import wraps

    @wraps(fn)
    def wrapper(*args, **kwargs):
        get_user_or_error()
        return fn(*args, **kwargs)
    return wrapper


def static(app: Any, url_path: str, local_path: str, *, browse: bool = False) -> None:
    """
    Register a static file path to be served by Caddy.

    The runner reads these registrations when starting Caddy.

    Args:
        app: Flask, FastAPI, or any WSGI/ASGI app instance
        url_path: URL path pattern (e.g., "/assets/*")
        local_path: Local filesystem path (e.g., "./static")
        browse: Enable directory listings
    """
    entry = StaticPath(url_path=url_path, local_path=local_path, browse=browse)

    framework = _detect_framework(app)
    if framework == "flask":
        if not hasattr(app, "extensions"):
            app.extensions = {}
        statics = app.extensions.setdefault("caddytail_static", [])
        statics.append(entry)
    elif framework == "fastapi":
        if not hasattr(app.state, "caddytail_static"):
            app.state.caddytail_static = []
        app.state.caddytail_static.append(entry)
    else:
        if not hasattr(app, "_caddytail_static"):
            app._caddytail_static = []
        app._caddytail_static.append(entry)


def get_registered_statics(app: Any) -> list[StaticPath]:
    """Read static() registrations from an app."""
    framework = _detect_framework(app)
    if framework == "flask":
        return list(getattr(app, "extensions", {}).get("caddytail_static", []))
    elif framework == "fastapi":
        return list(getattr(app.state, "caddytail_static", []))
    else:
        return list(getattr(app, "_caddytail_static", []))


def expose(
    app: Any,
    mode: str = "tailnet",
    *,
    port: int = 443,
    hostname: Optional[str] = None,
    auth: Optional[bool] = None,
) -> None:
    """Register an exposure (listener) for the app, read by the runner at startup.

    Mirrors :func:`static`. Lets app code declare funnel/tailnet intent without
    CLI flags::

        from caddytail import expose
        expose(app, "funnel-only", port=443)   # public, unauthenticated
        expose(app, "tailnet", port=8443)      # private, authenticated

    See :class:`Exposure` for the mode semantics.
    """
    entry = Exposure(mode=mode, port=port, hostname=hostname, auth=auth)

    framework = _detect_framework(app)
    if framework == "flask":
        if not hasattr(app, "extensions"):
            app.extensions = {}
        app.extensions.setdefault("caddytail_exposures", []).append(entry)
    elif framework == "fastapi":
        if not hasattr(app.state, "caddytail_exposures"):
            app.state.caddytail_exposures = []
        app.state.caddytail_exposures.append(entry)
    else:
        if not hasattr(app, "_caddytail_exposures"):
            app._caddytail_exposures = []
        app._caddytail_exposures.append(entry)


def get_registered_exposures(app: Any) -> list[Exposure]:
    """Read expose() registrations from an app."""
    framework = _detect_framework(app)
    if framework == "flask":
        return list(getattr(app, "extensions", {}).get("caddytail_exposures", []))
    elif framework == "fastapi":
        return list(getattr(app.state, "caddytail_exposures", []))
    else:
        return list(getattr(app, "_caddytail_exposures", []))


def apply_middleware(app: Any, framework: Optional[str] = None) -> Any:
    """Apply the framework-appropriate caddytail middleware and return the app.

    - Flask: ProxyFix (trust X-Forwarded-Host so redirects/url_for use the
      real tailnet/funnel hostname).
    - FastAPI/Starlette: middleware that stashes the Tailscale user on
      request.state.

    Used both by the in-process dev server and by the caddytail.wsgi shim,
    so an external server (gunicorn) gets the same behaviour after it
    re-imports the app in its worker processes.
    """
    if framework is None:
        framework = _detect_framework(app)

    if framework == "flask":
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_host=1)
    elif framework == "fastapi":
        from starlette.middleware.base import BaseHTTPMiddleware
        from starlette.requests import Request

        class TailscaleUserMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request: Request, call_next):
                request.state.tailscale_user = _extract_user_from_headers(
                    dict(request.headers)
                )
                return await call_next(request)

        app.add_middleware(TailscaleUserMiddleware)
    # Generic WSGI/ASGI callables need no middleware.
    return app


# ---------------------------------------------------------------------------
# CaddyTail class — for programmatic use
# ---------------------------------------------------------------------------

class CaddyTail:
    """
    Wrapper for any WSGI/ASGI app with a Tailscale-authenticated Caddy reverse proxy.

    Supports Flask, FastAPI, Django, and generic WSGI/ASGI callables.

    For most users, the CLI runner is simpler:
        caddytail run myapp app:app

    Args:
        app: Any WSGI or ASGI application (Flask, FastAPI, Django, bare callable, etc.)
        hostname: Tailscale hostname (e.g., "myapp")
        static: Extra static paths as a dict or list of StaticPath
        debug: Enable Caddy debug mode
        **kwargs: Override auto-detected settings (tailnet, caddy_path,
                  app_port, caddy_http_port, caddy_admin_port, state_dir)
    """

    def __init__(
        self,
        app: Any,
        hostname: str,
        *,
        static: Optional[Union[dict[str, str], list[StaticPath]]] = None,
        debug: bool = False,
        funnel: bool = False,
        tailnet_listener: bool = False,
        exposures: Optional[list[Exposure]] = None,
        server: str = "dev",
        workers: int = 1,
        threads: int = 8,
        app_ref: Optional[str] = None,
        tailnet: Optional[str] = None,
        caddy_path: Optional[Union[str, Path]] = None,
        app_port: Optional[int] = None,
        caddy_http_port: Optional[int] = None,
        caddy_admin_port: Optional[int] = None,
        state_dir: Optional[Union[str, Path]] = None,
    ):
        self.app = app
        self.hostname = hostname

        # App server: "dev" runs the app in-process (Werkzeug/wsgiref/uvicorn);
        # "gunicorn" spawns a hardened gunicorn subprocess bound to app_port.
        if server not in ("dev", "gunicorn"):
            raise ValueError(f"invalid server {server!r}; expected 'dev' or 'gunicorn'")
        self.server = server
        self.workers = workers
        self.threads = threads
        # Import string (module:variable) needed to launch an external server,
        # which re-imports the app in its own worker processes.
        self.app_ref = app_ref

        # Resolve which listeners to create. Priority:
        #   explicit exposures= > funnel/tailnet_listener flags >
        #   expose() registrations on the app > default (tailnet only).
        if exposures is not None:
            if funnel or tailnet_listener:
                raise ValueError(
                    "pass either exposures= or the funnel/tailnet_listener flags, not both"
                )
            self.exposures = list(exposures)
        elif funnel or tailnet_listener:
            self.exposures = _exposures_from_flags(funnel, tailnet_listener)
        else:
            registered = get_registered_exposures(app)
            self.exposures = list(registered) if registered else [Exposure("tailnet", 443)]

        # Auto-detect tailnet if not provided
        if tailnet is None:
            tailnet = get_tailnet_from_tailscale()
            if tailnet is None:
                raise ValueError(
                    "Could not auto-detect tailnet. Either provide the 'tailnet' argument "
                    "or ensure Tailscale is running and connected."
                )
        if tailnet.endswith(".ts.net"):
            tailnet = tailnet[:-7]
        self.tailnet = tailnet

        if caddy_path is None:
            self.caddy_path = Path(get_binary_path())
        else:
            self.caddy_path = Path(caddy_path).resolve()

        # Auto-allocate ports unless overridden
        self.app_port = app_port or _find_free_port()
        self.caddy_http_port = caddy_http_port or _find_free_port()
        self.caddy_admin_port = caddy_admin_port or _find_free_port()

        self.state_dir = Path(state_dir).resolve() if state_dir else None
        self.debug = debug

        # Normalize static paths
        self.static_paths: list[StaticPath] = []
        if static:
            if isinstance(static, dict):
                for url_path, local_path in static.items():
                    self.static_paths.append(StaticPath(url_path, local_path))
            else:
                self.static_paths = list(static)

        # Also pick up static() registrations
        self.static_paths.extend(get_registered_statics(app))

        self._caddy_process: Optional[subprocess.Popen] = None
        self._app_process: Optional[subprocess.Popen] = None
        self._framework = _detect_framework(app)

        # For the dev server we mutate the in-process app object. For an
        # external server the worker processes re-import the app, so the
        # middleware is re-applied there by the caddytail.wsgi shim instead.
        if self.server == "dev":
            self._setup_middleware()

    @property
    def admin_url(self) -> str:
        return f"http://localhost:{self.caddy_admin_port}"

    def _setup_middleware(self) -> None:
        apply_middleware(self.app, self._framework)

    def _exposure_url(self, exposure: Exposure) -> str:
        host = exposure.hostname or self.hostname
        url = f"https://{host}.{self.tailnet}.ts.net"
        if exposure.port != 443:
            url += f":{exposure.port}"
        return url

    @property
    def urls(self) -> list[tuple[Exposure, str]]:
        """The (exposure, URL) pairs for every listener this instance serves."""
        return [(e, self._exposure_url(e)) for e in self.exposures]

    @property
    def tailscale_url(self) -> str:
        """Primary URL — the first tailnet exposure, else the first exposure."""
        primary = next(
            (e for e in self.exposures if e.mode == "tailnet"),
            self.exposures[0],
        )
        return self._exposure_url(primary)

    def _build_subroutes(self, exposure: Exposure) -> list[dict]:
        """Static-file routes plus the terminal reverse_proxy for one exposure.

        Static paths are shared across all exposures. The reverse_proxy either
        injects the trusted Tailscale identity headers (authenticated tailnet
        exposures) or *strips* them (funnel exposures) — on an unauthenticated
        public listener a client could otherwise forge ``Tailscale-User-*``
        headers and impersonate any user.
        """
        routes: list[dict] = []

        for sp in self.static_paths:
            local_path = str(Path(sp.local_path).resolve())
            match = [{"path": [sp.url_path]}]
            if sp.methods:
                match[0]["method"] = sp.methods

            handler = {
                "handler": "file_server",
                "root": local_path,
            }
            if sp.browse:
                handler["browse"] = {}

            routes.append({
                "match": match,
                "handle": [handler],
            })

        if exposure.auth:
            request_headers = {
                "set": {
                    HEADER_USER_LOGIN: ["{http.auth.user.tailscale_login}"],
                    HEADER_USER_NAME: ["{http.auth.user.tailscale_name}"],
                    HEADER_USER_PROFILE_PIC: ["{http.auth.user.tailscale_profile_picture}"],
                    HEADER_NODE: ["{http.auth.user.tailscale_node}"],
                    HEADER_TAGS: ["{http.auth.user.tailscale_tags}"],
                }
            }
        else:
            request_headers = {
                "delete": [HEADER_USER_NAME, HEADER_USER_LOGIN,
                           HEADER_USER_PROFILE_PIC, HEADER_NODE, HEADER_TAGS],
            }

        routes.append({
            "handle": [{
                "handler": "reverse_proxy",
                "upstreams": [{"dial": f"localhost:{self.app_port}"}],
                "headers": {"request": request_headers},
            }],
        })

        return routes

    def _build_server(self, exposure: Exposure) -> dict:
        """Build one Caddy HTTP server for a single exposure."""
        host = exposure.hostname or self.hostname
        fqdn = f"{host}.{self.tailnet}.ts.net"

        handlers: list[dict] = []
        if exposure.auth:
            handlers.append({
                "handler": "authentication",
                "providers": {"tailscale": {}},
            })
        handlers.append({
            "handler": "encode",
            "encodings": {"zstd": {}, "gzip": {}},
            "prefer": ["zstd", "gzip"],
        })
        handlers.append({
            "handler": "subroute",
            "routes": self._build_subroutes(exposure),
        })

        server: dict[str, Any] = {
            "listen": [f"{exposure.network}/{host}:{exposure.port}"],
            # Funnel listeners are TCP-only; HTTP/3 (UDP) is not available.
            "protocols": ["h1", "h2"] if exposure.is_funnel else ["h1", "h2", "h3"],
            "routes": [
                {"match": [{"host": [fqdn]}]},
                {"handle": handlers},
            ],
        }
        if exposure.is_funnel:
            # tsnet's funnel listener already terminates TLS; Caddy must not
            # try to manage certs or redirect HTTP for this listener.
            server["automatic_https"] = {"disable": True}

        return server

    def generate_config(self) -> dict:
        """Generate Caddy JSON configuration."""
        servers = {}
        for exposure in self.exposures:
            name = f"{exposure.mode.replace('-', '_')}_{exposure.port}"
            servers[name] = self._build_server(exposure)

        # One TLS automation policy per authenticated tailnet FQDN. Funnel
        # listeners self-terminate TLS via tsnet, so they get no policy.
        tailnet_fqdns = sorted({
            f"{(e.hostname or self.hostname)}.{self.tailnet}.ts.net"
            for e in self.exposures
            if not e.is_funnel
        })

        # All distinct nodes referenced by the exposures.
        node_hosts = sorted({e.hostname or self.hostname for e in self.exposures})

        config: dict[str, Any] = {
            "admin": {
                "listen": f"localhost:{self.caddy_admin_port}",
                "config": {"persist": False},
            },
            "apps": {
                "http": {
                    "http_port": self.caddy_http_port,
                    "servers": servers,
                },
                "tailscale": {
                    **({"state_dir": str(self.state_dir)} if self.state_dir else {}),
                    "nodes": {host: {} for host in node_hosts},
                },
            },
        }

        if tailnet_fqdns:
            config["apps"]["tls"] = {
                "automation": {
                    "policies": [{
                        "subjects": tailnet_fqdns,
                        "get_certificate": [{"via": "tailscale"}],
                    }],
                },
            }

        if self.debug:
            config["logging"] = {
                "logs": {"default": {"level": "DEBUG"}}
            }

        return config

    def _api_request(
        self,
        path: str,
        method: str = "GET",
        data: Optional[dict] = None,
        timeout: float = 10.0,
    ) -> dict:
        url = f"{self.admin_url}{path}"
        headers = {"Content-Type": "application/json"}
        body = json.dumps(data).encode() if data else None
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                response_body = resp.read().decode()
                if response_body:
                    return json.loads(response_body)
                return {}
        except urllib.error.HTTPError as e:
            error_body = e.read().decode() if e.fp else ""
            raise RuntimeError(f"Caddy API error {e.code}: {error_body}") from e

    def _wait_for_admin_api(self, timeout: float = 30.0, interval: float = 0.5) -> bool:
        start = time.time()
        while time.time() - start < timeout:
            try:
                self._api_request("/config/", timeout=2.0)
                return True
            except (urllib.error.URLError, ConnectionRefusedError, OSError):
                time.sleep(interval)
        return False

    def _print_urls(self) -> None:
        print("Configuration loaded. URLs:")
        for exposure, url in self.urls:
            label = "public" if exposure.is_funnel else "tailnet"
            print(f"  [{label}] {url}")

    def load_config(self) -> None:
        config = self.generate_config()
        self._api_request("/load", method="POST", data=config)
        self._print_urls()

    def reload_config(self) -> None:
        self.load_config()

    def get_current_config(self) -> dict:
        return self._api_request("/config/")

    def add_static_path(self, url_path: str, local_path: str, methods: Optional[list[str]] = None) -> None:
        self.static_paths.append(StaticPath(
            url_path=url_path,
            local_path=local_path,
            methods=methods or ["GET", "HEAD"],
        ))
        if self._caddy_process is not None:
            self.load_config()

    def remove_static_path(self, url_path: str) -> bool:
        for i, sp in enumerate(self.static_paths):
            if sp.url_path == url_path:
                self.static_paths.pop(i)
                if self._caddy_process is not None:
                    self.load_config()
                return True
        return False

    def _ensure_tailscale_auth(self) -> None:
        """Run caddy tailscale-auth as a pre-flight check before loading config.

        If the node is already authenticated, this returns immediately.
        Otherwise it displays the auth URL and blocks until authenticated.

        When no state_dir is set, both this and the caddy-tailscale plugin
        default to ~/.config/tsnet-caddy-{hostname}.

        Runs once per distinct node referenced by the exposures.
        """
        node_hosts = sorted({e.hostname or self.hostname for e in self.exposures})
        for host in node_hosts:
            cmd = [
                str(self.caddy_path), "tailscale-auth",
                "--hostname", host,
            ]
            if self.state_dir:
                cmd.extend(["--state-dir", str(self.state_dir / host)])
            rc = subprocess.call(cmd)
            if rc != 0:
                raise RuntimeError(
                    f"Tailscale authentication failed for '{host}' (exit code {rc})"
                )

    def start_caddy(self) -> subprocess.Popen:
        if self._caddy_process is not None:
            raise RuntimeError("Caddy is already running")

        if sys.platform != "win32":
            os.chmod(self.caddy_path, 0o755)

        self._ensure_tailscale_auth()

        config = self.generate_config()

        # The tailscale-run command reads one JSON config from stdin using a
        # streaming decoder (no EOF required), starts Caddy, then watches
        # stdin for parent death.
        cmd = [str(self.caddy_path), "tailscale-run"]

        print(f"Starting Caddy with admin API on port {self.caddy_admin_port}...")

        self._caddy_process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=sys.stdout,
            stderr=sys.stderr,
        )

        self._caddy_process.stdin.write(json.dumps(config).encode())
        self._caddy_process.stdin.flush()

        if not self._wait_for_admin_api():
            self.stop_caddy()
            raise RuntimeError("Caddy admin API did not become available")

        self._print_urls()

        return self._caddy_process

    def stop_caddy(self) -> None:
        if self._caddy_process is not None:
            # Close stdin to signal the caddy-tailscale watch_stdin goroutine.
            if self._caddy_process.stdin:
                try:
                    self._caddy_process.stdin.close()
                except OSError:
                    pass

            try:
                self._api_request("/stop", method="POST", timeout=2.0)
            except (urllib.error.URLError, RuntimeError, OSError):
                pass

            self._caddy_process.terminate()
            try:
                self._caddy_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._caddy_process.kill()
            self._caddy_process = None

    def _gunicorn_command(self) -> list[str]:
        """Build the gunicorn command line for the current framework.

        WSGI apps use the ``gthread`` worker (one process, N threads) — a
        drop-in for the threaded dev server that keeps in-memory state
        consistent. ASGI apps use the uvicorn worker class instead.
        """
        if not self.app_ref:
            raise RuntimeError(
                "server='gunicorn' requires app_ref (the 'module:variable' "
                "import string); it is set automatically by `caddytail run`"
            )

        import importlib.util
        if importlib.util.find_spec("gunicorn") is None:
            raise RuntimeError(
                "gunicorn is not installed. Install it with: "
                "pip install 'caddytail[gunicorn]'"
            )

        cmd = [
            sys.executable, "-m", "gunicorn",
            "--bind", f"127.0.0.1:{self.app_port}",
            "--workers", str(self.workers),
        ]
        if self._framework in ("fastapi", "asgi"):
            if importlib.util.find_spec("uvicorn") is None:
                raise RuntimeError(
                    "serving an ASGI app under gunicorn needs uvicorn. Install "
                    "it with: pip install 'caddytail[gunicorn]'"
                )
            cmd += ["--worker-class", "uvicorn.workers.UvicornWorker"]
        else:
            cmd += ["--worker-class", "gthread", "--threads", str(self.threads)]
        cmd.append("caddytail.wsgi:application")
        return cmd

    def _start_app_server(self) -> subprocess.Popen:
        """Spawn gunicorn as a managed subprocess bound to app_port."""
        if self._app_process is not None:
            raise RuntimeError("App server is already running")

        cmd = self._gunicorn_command()
        env = os.environ.copy()
        # The caddytail.wsgi shim reads this to import + wrap the real app.
        env["CADDYTAIL_APP"] = self.app_ref

        print(f"Starting app server: {' '.join(cmd)}")
        self._app_process = subprocess.Popen(
            cmd, env=env, stdout=sys.stdout, stderr=sys.stderr,
        )
        return self._app_process

    def _stop_app_server(self) -> None:
        if self._app_process is not None:
            # SIGTERM triggers gunicorn's graceful worker shutdown.
            self._app_process.terminate()
            try:
                self._app_process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self._app_process.kill()
            self._app_process = None

    def run(
        self,
        host: str = "127.0.0.1",
        start_caddy: bool = True,
        **kwargs,
    ) -> None:
        def signal_handler(signum, frame):
            print("\nShutting down...")
            self._stop_app_server()
            self.stop_caddy()
            sys.exit(0)

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        if start_caddy:
            self.start_caddy()

        try:
            if self.server == "gunicorn":
                proc = self._start_app_server()
                proc.wait()
            elif self._framework == "flask":
                self.app.run(host=host, port=self.app_port, **kwargs)
            elif self._framework == "wsgi":
                from wsgiref.simple_server import make_server
                httpd = make_server(host, self.app_port, self.app)
                httpd.serve_forever()
            else:
                # fastapi and asgi both use uvicorn
                import uvicorn
                uvicorn.run(self.app, host=host, port=self.app_port, **kwargs)
        finally:
            self._stop_app_server()
            self.stop_caddy()

    def run_async(
        self,
        host: str = "127.0.0.1",
        start_caddy: bool = True,
    ) -> tuple[Optional[subprocess.Popen], threading.Thread]:
        caddy_proc = None
        if start_caddy:
            caddy_proc = self.start_caddy()

        def run_app():
            if self._framework == "flask":
                self.app.run(host=host, port=self.app_port, threaded=True)
            elif self._framework == "wsgi":
                from wsgiref.simple_server import make_server
                httpd = make_server(host, self.app_port, self.app)
                httpd.serve_forever()
            else:
                # fastapi and asgi both use uvicorn
                import uvicorn
                uvicorn.run(self.app, host=host, port=self.app_port)

        app_thread = threading.Thread(target=run_app, daemon=True)
        app_thread.start()

        return caddy_proc, app_thread


# ---------------------------------------------------------------------------
# Legacy helpers — kept for backwards compatibility
# ---------------------------------------------------------------------------

def flask_user_required(caddy: CaddyTail):
    """Decorator that injects the Tailscale user into Flask route handlers.

    Deprecated: use the standalone login_required instead.
    """
    from functools import wraps
    from flask import g

    def decorator(f: Callable) -> Callable:
        @wraps(f)
        def wrapper(*args, **kwargs):
            user = get_user()
            if user is None:
                from flask import abort
                abort(401, "Tailscale authentication required")
            g.tailscale_user = user.to_dict()
            return f(*args, **kwargs)
        return wrapper
    return decorator


def fastapi_user_dependency(caddy: CaddyTail):
    """Create a FastAPI dependency that provides the Tailscale user.

    Deprecated: use the standalone login_required instead.
    """
    from fastapi import Request

    async def get_tailscale_user(request: Request) -> dict[str, str]:
        user = get_user_or_error(request)
        return user.to_dict()

    return get_tailscale_user
