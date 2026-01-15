"""
Tailscale + Caddy reverse proxy wrapper for Flask and FastAPI applications.

This module provides easy integration of Flask/FastAPI apps with a Caddy reverse proxy
that handles Tailscale authentication and static file serving.

Uses Caddy's admin API to configure the server dynamically (no config files needed).

Usage with Flask:
    from flask import Flask
    from caddytail import CaddyTail

    app = Flask(__name__)
    caddy = CaddyTail(
        app,
        hostname="myapp",
        tailnet="mytailnet",
        static_paths={"/static/*": "./static"},
    )

    @app.get("/")
    def index():
        user = caddy.get_user()  # Returns dict with name, login, profile_pic
        return f"Hello, {user['name']}!"

    if __name__ == "__main__":
        caddy.run()  # Starts both Caddy and the Flask app

Usage with FastAPI:
    from fastapi import FastAPI, Request
    from caddytail import CaddyTail

    app = FastAPI()
    caddy = CaddyTail(
        app,
        hostname="myapp",
        tailnet="mytailnet",
    )

    @app.get("/")
    async def index(request: Request):
        user = caddy.get_user(request)
        return {"message": f"Hello, {user['name']}!"}

    if __name__ == "__main__":
        caddy.run()
"""

from __future__ import annotations

import json
import signal
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


class CaddyTail:
    """
    Wrapper for Flask/FastAPI apps that sets up a Tailscale-authenticated Caddy reverse proxy.

    Uses Caddy's admin API to configure the server dynamically.

    Args:
        app: Flask or FastAPI application instance
        hostname: Tailscale hostname (e.g., "myapp" -> myapp.tailnet.ts.net)
        tailnet: Tailscale tailnet name (without .ts.net suffix)
        caddy_path: Path to caddy binary (default: uses bundled binary)
        app_port: Port for the Python app to listen on (default: 10800)
        caddy_http_port: Port for Caddy's HTTP listener (default: 10102)
        caddy_admin_port: Port for Caddy's admin API (default: 2019)
        static_paths: Dict mapping URL patterns to local paths, or list of StaticPath objects
        state_dir: Directory for Tailscale state (default: "./tailscale-state")
        debug: Enable Caddy debug mode
    """

    HEADER_USER_NAME = "Tailscale-User-Name"
    HEADER_USER_LOGIN = "Tailscale-User-Login"
    HEADER_USER_PROFILE_PIC = "Tailscale-User-Profile-Pic"

    def __init__(
        self,
        app: Union[FlaskApp, FastAPIApp],
        hostname: str,
        tailnet: str,
        caddy_path: Optional[Union[str, Path]] = None,
        app_port: int = 10800,
        caddy_http_port: int = 10102,
        caddy_admin_port: int = 2019,
        static_paths: Optional[Union[dict[str, str], list[StaticPath]]] = None,
        state_dir: Union[str, Path] = "./tailscale-state",
        debug: bool = False,
    ):
        self.app = app
        self.hostname = hostname
        self.tailnet = tailnet

        # Use bundled binary by default
        if caddy_path is None:
            self.caddy_path = Path(get_binary_path())
        else:
            self.caddy_path = Path(caddy_path).resolve()

        self.app_port = app_port
        self.caddy_http_port = caddy_http_port
        self.caddy_admin_port = caddy_admin_port
        self.state_dir = Path(state_dir).resolve()
        self.debug = debug

        # Normalize static paths
        self.static_paths: list[StaticPath] = []
        if static_paths:
            if isinstance(static_paths, dict):
                for url_path, local_path in static_paths.items():
                    self.static_paths.append(StaticPath(url_path, local_path))
            else:
                self.static_paths = list(static_paths)

        self._caddy_process: Optional[subprocess.Popen] = None
        self._framework = self._detect_framework()

        # Install middleware for user injection
        self._setup_middleware()

    @property
    def admin_url(self) -> str:
        """Get the Caddy admin API URL."""
        return f"http://localhost:{self.caddy_admin_port}"

    def _detect_framework(self) -> str:
        """Detect whether we're working with Flask or FastAPI."""
        app_module = type(self.app).__module__
        if "flask" in app_module:
            return "flask"
        elif "fastapi" in app_module or "starlette" in app_module:
            return "fastapi"
        else:
            # Try duck typing
            if hasattr(self.app, "wsgi_app"):
                return "flask"
            elif hasattr(self.app, "add_middleware"):
                return "fastapi"
            raise ValueError(f"Unknown app framework: {app_module}")

    def _setup_middleware(self) -> None:
        """Set up framework-specific middleware for user context."""
        if self._framework == "flask":
            self._setup_flask_middleware()
        else:
            self._setup_fastapi_middleware()

    def _setup_flask_middleware(self) -> None:
        """Set up Flask middleware using ProxyFix and request context."""
        from werkzeug.middleware.proxy_fix import ProxyFix
        self.app.wsgi_app = ProxyFix(self.app.wsgi_app, x_host=1)

    def _setup_fastapi_middleware(self) -> None:
        """Set up FastAPI/Starlette middleware for user context."""
        from starlette.middleware.base import BaseHTTPMiddleware
        from starlette.requests import Request

        caddy = self

        class TailscaleUserMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request: Request, call_next):
                # Store user info in request state for easy access
                user = caddy._extract_user_from_headers(dict(request.headers))
                request.state.tailscale_user = user
                return await call_next(request)

        self.app.add_middleware(TailscaleUserMiddleware)

    def _extract_user_from_headers(self, headers: dict[str, str]) -> Optional[TailscaleUser]:
        """Extract Tailscale user from request headers."""
        # Case-insensitive header lookup
        headers_lower = {k.lower(): v for k, v in headers.items()}

        name_header = headers_lower.get(self.HEADER_USER_NAME.lower())
        if not name_header:
            return None

        # Decode the name which may be latin1-encoded UTF-8
        try:
            name = name_header.encode('latin1').decode('utf8')
        except (UnicodeDecodeError, UnicodeEncodeError):
            name = name_header

        return TailscaleUser(
            name=name,
            login=headers_lower.get(self.HEADER_USER_LOGIN.lower(), ""),
            profile_pic=headers_lower.get(self.HEADER_USER_PROFILE_PIC.lower(), ""),
        )

    def get_user(self, request: Optional[StarletteRequest] = None) -> Optional[dict[str, str]]:
        """
        Get the authenticated Tailscale user.

        For Flask: Call with no arguments (uses flask.request)
        For FastAPI: Pass the Request object

        Returns:
            Dict with 'name', 'login', 'profile_pic' keys, or None if not authenticated
        """
        if self._framework == "flask":
            from flask import request as flask_request
            user = self._extract_user_from_headers(dict(flask_request.headers))
        else:
            if request is None:
                raise ValueError("FastAPI requires passing the request object to get_user()")
            # Try request.state first (set by middleware), fall back to headers
            if hasattr(request.state, "tailscale_user"):
                user = request.state.tailscale_user
            else:
                user = self._extract_user_from_headers(dict(request.headers))

        return user.to_dict() if user else None

    def get_user_or_error(self, request: Optional[StarletteRequest] = None) -> dict[str, str]:
        """Like get_user() but raises an error if not authenticated."""
        user = self.get_user(request)
        if user is None:
            if self._framework == "flask":
                from flask import abort
                abort(401, "Tailscale authentication required")
            else:
                from fastapi import HTTPException
                raise HTTPException(status_code=401, detail="Tailscale authentication required")
        return user

    @property
    def tailscale_url(self) -> str:
        """Get the full Tailscale HTTPS URL."""
        return f"https://{self.hostname}.{self.tailnet}.ts.net"

    def generate_config(self) -> dict:
        """Generate Caddy JSON configuration."""
        # Build route handlers
        routes = []

        # Static file handlers
        for static in self.static_paths:
            local_path = str(Path(static.local_path).resolve())

            # Build matcher for this static path
            match = [{"path": [static.url_path]}]
            if static.methods:
                match[0]["method"] = static.methods

            routes.append({
                "match": match,
                "handle": [
                    {
                        "handler": "file_server",
                        "root": local_path,
                    }
                ],
            })

        # Reverse proxy handler (catch-all)
        routes.append({
            "handle": [
                {
                    "handler": "reverse_proxy",
                    "upstreams": [{"dial": f"localhost:{self.app_port}"}],
                    "headers": {
                        "request": {
                            "set": {
                                self.HEADER_USER_LOGIN: ["{http.auth.user.tailscale_login}"],
                                self.HEADER_USER_NAME: ["{http.auth.user.tailscale_name}"],
                                self.HEADER_USER_PROFILE_PIC: ["{http.auth.user.tailscale_profile_picture}"],
                            }
                        }
                    },
                }
            ],
        })

        # Build the full config
        config = {
            "admin": {
                "listen": f"localhost:{self.caddy_admin_port}",
            },
            "apps": {
                "http": {
                    "http_port": self.caddy_http_port,
                    "servers": {
                        "tailscale_srv": {
                            "listen": [f"tailscale/{self.hostname}"],
                            "routes": [
                                {
                                    "handle": [
                                        {
                                            "handler": "authentication",
                                            "providers": {
                                                "tailscale": {},
                                            },
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
                "tailscale": {
                    "apps": {
                        self.hostname: {},
                    },
                },
            },
        }

        if self.debug:
            config["logging"] = {
                "logs": {
                    "default": {
                        "level": "DEBUG",
                    }
                }
            }

        return config

    def _api_request(
        self,
        path: str,
        method: str = "GET",
        data: Optional[dict] = None,
        timeout: float = 10.0,
    ) -> dict:
        """Make a request to Caddy's admin API."""
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
        """Wait for Caddy's admin API to become available."""
        start = time.time()
        while time.time() - start < timeout:
            try:
                self._api_request("/config/", timeout=2.0)
                return True
            except (urllib.error.URLError, ConnectionRefusedError, OSError):
                time.sleep(interval)
        return False

    def load_config(self) -> None:
        """Load configuration into running Caddy via admin API."""
        config = self.generate_config()
        self._api_request("/load", method="POST", data=config)
        print(f"Configuration loaded. Tailscale URL: {self.tailscale_url}")

    def reload_config(self) -> None:
        """Reload configuration (alias for load_config)."""
        self.load_config()

    def get_current_config(self) -> dict:
        """Get current Caddy configuration from admin API."""
        return self._api_request("/config/")

    def add_static_path(self, url_path: str, local_path: str, methods: Optional[list[str]] = None) -> None:
        """
        Add a new static path and reload config.

        Args:
            url_path: URL path pattern (e.g., "/static/*")
            local_path: Local filesystem path
            methods: HTTP methods to match (default: ["GET"])
        """
        self.static_paths.append(StaticPath(
            url_path=url_path,
            local_path=local_path,
            methods=methods or ["GET"],
        ))
        if self._caddy_process is not None:
            self.load_config()

    def remove_static_path(self, url_path: str) -> bool:
        """
        Remove a static path by URL pattern and reload config.

        Returns:
            True if path was found and removed, False otherwise
        """
        for i, static in enumerate(self.static_paths):
            if static.url_path == url_path:
                self.static_paths.pop(i)
                if self._caddy_process is not None:
                    self.load_config()
                return True
        return False

    def start_caddy(self) -> subprocess.Popen:
        """Start the Caddy process and load configuration via API."""
        if self._caddy_process is not None:
            raise RuntimeError("Caddy is already running")

        # Ensure state directory exists
        self.state_dir.mkdir(parents=True, exist_ok=True)

        # Ensure the binary is executable on Unix-like systems
        if sys.platform != "win32":
            import os
            os.chmod(self.caddy_path, 0o755)

        # Start Caddy with minimal config (just admin API)
        # The full config will be loaded via the API
        minimal_config = json.dumps({
            "admin": {
                "listen": f"localhost:{self.caddy_admin_port}",
            }
        })

        cmd = [str(self.caddy_path), "run", "--config", "-", "--adapter", ""]

        print(f"Starting Caddy with admin API on port {self.caddy_admin_port}...")

        self._caddy_process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=sys.stdout,
            stderr=sys.stderr,
        )

        # Send minimal config via stdin
        self._caddy_process.stdin.write(minimal_config.encode())
        self._caddy_process.stdin.close()

        # Wait for admin API to be ready
        if not self._wait_for_admin_api():
            self.stop_caddy()
            raise RuntimeError("Caddy admin API did not become available")

        # Load the full configuration
        self.load_config()

        return self._caddy_process

    def stop_caddy(self) -> None:
        """Stop the Caddy process."""
        if self._caddy_process is not None:
            # Try graceful shutdown via API first
            try:
                self._api_request("/stop", method="POST", timeout=2.0)
            except (urllib.error.URLError, RuntimeError, OSError):
                pass

            # Then terminate the process
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
        """
        Run both Caddy and the Python application.

        Args:
            host: Host to bind the Python app to (default: 127.0.0.1)
            start_caddy: Whether to start Caddy (default: True)
            **kwargs: Additional arguments passed to the framework's run method
        """
        # Set up signal handlers for clean shutdown
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
                # Flask's built-in server
                self.app.run(host=host, port=self.app_port, **kwargs)
            else:
                # FastAPI with uvicorn
                import uvicorn
                uvicorn.run(self.app, host=host, port=self.app_port, **kwargs)
        finally:
            self.stop_caddy()

    def run_async(
        self,
        host: str = "127.0.0.1",
        start_caddy: bool = True,
    ) -> tuple[Optional[subprocess.Popen], threading.Thread]:
        """
        Start Caddy and the app in background threads.

        Returns:
            Tuple of (caddy_process, app_thread)
        """
        caddy_proc = None
        if start_caddy:
            caddy_proc = self.start_caddy()

        def run_app():
            if self._framework == "flask":
                self.app.run(host=host, port=self.app_port, threaded=True)
            else:
                import uvicorn
                uvicorn.run(self.app, host=host, port=self.app_port)

        app_thread = threading.Thread(target=run_app, daemon=True)
        app_thread.start()

        return caddy_proc, app_thread


def flask_user_required(caddy: CaddyTail):
    """Decorator that injects the Tailscale user into Flask route handlers."""
    from functools import wraps
    from flask import g

    def decorator(f: Callable) -> Callable:
        @wraps(f)
        def wrapper(*args, **kwargs):
            g.tailscale_user = caddy.get_user_or_error()
            return f(*args, **kwargs)
        return wrapper
    return decorator


def fastapi_user_dependency(caddy: CaddyTail):
    """Create a FastAPI dependency that provides the Tailscale user."""
    from fastapi import Request

    async def get_tailscale_user(request: Request) -> dict[str, str]:
        return caddy.get_user_or_error(request)

    return get_tailscale_user
