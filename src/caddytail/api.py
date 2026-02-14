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

import atexit
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
    methods: list[str] = field(default_factory=lambda: ["GET"])


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


def static(app: Any, url_path: str, local_path: str) -> None:
    """
    Register a static file path to be served by Caddy.

    The runner reads these registrations when starting Caddy.

    Args:
        app: Flask, FastAPI, or any WSGI/ASGI app instance
        url_path: URL path pattern (e.g., "/assets/*")
        local_path: Local filesystem path (e.g., "./static")
    """
    entry = StaticPath(url_path=url_path, local_path=local_path)

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
        tailnet: Optional[str] = None,
        caddy_path: Optional[Union[str, Path]] = None,
        app_port: Optional[int] = None,
        caddy_http_port: Optional[int] = None,
        caddy_admin_port: Optional[int] = None,
        state_dir: Union[str, Path] = "./tailscale-state",
    ):
        self.app = app
        self.hostname = hostname

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

        self.state_dir = Path(state_dir).resolve()
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
        self._watchdog_process: Optional[subprocess.Popen] = None
        self._watchdog_pipe_write: Optional[int] = None
        self._framework = _detect_framework(app)

        self._setup_middleware()

    @property
    def admin_url(self) -> str:
        return f"http://localhost:{self.caddy_admin_port}"

    def _setup_middleware(self) -> None:
        if self._framework == "flask":
            self._setup_flask_middleware()
        elif self._framework == "fastapi":
            self._setup_fastapi_middleware()
        # No middleware needed for generic wsgi/asgi apps

    def _setup_flask_middleware(self) -> None:
        from werkzeug.middleware.proxy_fix import ProxyFix
        self.app.wsgi_app = ProxyFix(self.app.wsgi_app, x_host=1)

    def _setup_fastapi_middleware(self) -> None:
        from starlette.middleware.base import BaseHTTPMiddleware
        from starlette.requests import Request

        class TailscaleUserMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request: Request, call_next):
                user = _extract_user_from_headers(dict(request.headers))
                request.state.tailscale_user = user
                return await call_next(request)

        self.app.add_middleware(TailscaleUserMiddleware)

    @property
    def tailscale_url(self) -> str:
        return f"https://{self.hostname}.{self.tailnet}.ts.net"

    def generate_config(self) -> dict:
        """Generate Caddy JSON configuration."""
        routes = []

        for sp in self.static_paths:
            local_path = str(Path(sp.local_path).resolve())
            match = [{"path": [sp.url_path]}]
            if sp.methods:
                match[0]["method"] = sp.methods

            routes.append({
                "match": match,
                "handle": [{
                    "handler": "file_server",
                    "root": local_path,
                }],
            })

        routes.append({
            "handle": [{
                "handler": "reverse_proxy",
                "upstreams": [{"dial": f"localhost:{self.app_port}"}],
                "headers": {
                    "request": {
                        "set": {
                            HEADER_USER_LOGIN: ["{http.auth.user.tailscale_login}"],
                            HEADER_USER_NAME: ["{http.auth.user.tailscale_name}"],
                            HEADER_USER_PROFILE_PIC: ["{http.auth.user.tailscale_profile_picture}"],
                        }
                    }
                },
            }],
        })

        fqdn = f"{self.hostname}.{self.tailnet}.ts.net"

        config: dict[str, Any] = {
            "admin": {
                "listen": f"localhost:{self.caddy_admin_port}",
            },
            "apps": {
                "http": {
                    "http_port": self.caddy_http_port,
                    "servers": {
                        "tailscale_srv": {
                            "listen": [f"tailscale/{self.hostname}:443"],
                            "routes": [
                                {
                                    "match": [{"host": [fqdn]}],
                                },
                                {
                                    "handle": [
                                        {
                                            "handler": "authentication",
                                            "providers": {"tailscale": {}},
                                        },
                                        {
                                            "handler": "subroute",
                                            "routes": routes,
                                        },
                                    ],
                                }
                            ],
                        }
                    },
                },
                "tls": {
                    "automation": {
                        "policies": [{
                            "subjects": [fqdn],
                            "get_certificate": [{"via": "tailscale"}],
                        }],
                    },
                },
                "tailscale": {
                    "nodes": {self.hostname: {}},
                },
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

    def load_config(self) -> None:
        config = self.generate_config()
        self._api_request("/load", method="POST", data=config)
        print(f"Configuration loaded. Tailscale URL: {self.tailscale_url}")

    def reload_config(self) -> None:
        self.load_config()

    def get_current_config(self) -> dict:
        return self._api_request("/config/")

    def add_static_path(self, url_path: str, local_path: str, methods: Optional[list[str]] = None) -> None:
        self.static_paths.append(StaticPath(
            url_path=url_path,
            local_path=local_path,
            methods=methods or ["GET"],
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

    def _start_watchdog(self) -> None:
        if self._caddy_process is None:
            raise RuntimeError("Cannot start watchdog: Caddy is not running")

        read_fd, write_fd = os.pipe()
        watchdog_module = Path(__file__).parent / "watchdog.py"
        caddy_pid = self._caddy_process.pid
        cmd = [
            sys.executable,
            str(watchdog_module),
            str(read_fd),
            str(self.caddy_admin_port),
            str(caddy_pid),
        ]
        self._watchdog_process = subprocess.Popen(
            cmd,
            pass_fds=(read_fd,),
            stdout=sys.stderr,
            stderr=sys.stderr,
        )
        os.close(read_fd)
        self._watchdog_pipe_write = write_fd

    def _stop_watchdog(self) -> None:
        if self._watchdog_pipe_write is not None:
            try:
                os.close(self._watchdog_pipe_write)
            except OSError:
                pass
            self._watchdog_pipe_write = None

        if self._watchdog_process is not None:
            try:
                self._watchdog_process.terminate()
                self._watchdog_process.wait(timeout=2)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    self._watchdog_process.kill()
                except OSError:
                    pass
            self._watchdog_process = None

    def start_caddy(self) -> subprocess.Popen:
        if self._caddy_process is not None:
            raise RuntimeError("Caddy is already running")

        self.state_dir.mkdir(parents=True, exist_ok=True)

        if sys.platform != "win32":
            os.chmod(self.caddy_path, 0o755)

        minimal_config = json.dumps({
            "admin": {"listen": f"localhost:{self.caddy_admin_port}"}
        })

        cmd = [str(self.caddy_path), "run", "--config", "-", "--adapter", ""]

        print(f"Starting Caddy with admin API on port {self.caddy_admin_port}...")

        self._caddy_process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=sys.stdout,
            stderr=sys.stderr,
        )

        self._caddy_process.stdin.write(minimal_config.encode())
        self._caddy_process.stdin.close()

        if not self._wait_for_admin_api():
            self.stop_caddy()
            raise RuntimeError("Caddy admin API did not become available")

        self.load_config()
        self._start_watchdog()
        atexit.register(self._stop_watchdog)

        return self._caddy_process

    def stop_caddy(self) -> None:
        self._stop_watchdog()

        if self._caddy_process is not None:
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

    def run(
        self,
        host: str = "127.0.0.1",
        start_caddy: bool = True,
        **kwargs,
    ) -> None:
        def signal_handler(signum, frame):
            print("\nShutting down...")
            self.stop_caddy()
            sys.exit(0)

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        if start_caddy:
            self.start_caddy()

        try:
            if self._framework == "flask":
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
