"""Caddy web server with Tailscale plugin, packaged for pip installation."""

import os
import subprocess
import sys

__version__ = "0.3.0"

_PYPI_URL = "https://pypi.org/pypi/caddytail/json"

USAGE = """\
usage: caddytail <command> [args...]

commands:
  run <hostname> <app_ref>     Run app in foreground (Ctrl-C kills everything)
  install <hostname> <app_ref> Install as systemd service + tail logs
  status <hostname>            Show service status
  logs <hostname> [-n N] [-f]  Show service logs
  restart <hostname>           Restart service
  uninstall <hostname>         Stop + remove service
  list                         List installed services
  caddy [args...]              Raw Caddy pass-through
"""


def _bundled_binary_path() -> str:
    """Return the path where the bundled caddy binary should live."""
    package_dir = os.path.dirname(__file__)
    binary_name = "caddy.exe" if sys.platform == "win32" else "caddy"
    return os.path.join(package_dir, "bin", binary_name)


def fetch_binary() -> str:
    """Download the caddy binary from the latest PyPI release.

    Downloads the platform-specific wheel from PyPI and extracts the
    caddy binary into the package's ``bin/`` directory.  Uses only the
    standard library — no external tools required.

    Returns the path to the downloaded binary.

    Raises RuntimeError if the download fails.
    """
    import json
    import platform
    import stat
    import urllib.request
    import urllib.error
    import zipfile
    from io import BytesIO
    from pathlib import Path

    system = platform.system().lower()
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        machine = "x86_64"
    elif machine in ("aarch64", "arm64"):
        machine = "aarch64"

    tag = {
        ("linux", "x86_64"): "manylinux2014_x86_64",
        ("linux", "aarch64"): "manylinux2014_aarch64",
        ("darwin", "x86_64"): "macosx_10_15_x86_64",
        ("darwin", "aarch64"): "macosx_11_0_arm64",
        ("windows", "x86_64"): "win_amd64",
    }.get((system, machine))
    if tag is None:
        raise RuntimeError(f"Unsupported platform: {system} {machine}")

    binary_name = "caddy.exe" if platform.system() == "Windows" else "caddy"
    dest = Path(_bundled_binary_path())

    print(f"Caddy binary not found — downloading from PyPI (platform: {tag})...")

    try:
        with urllib.request.urlopen(_PYPI_URL) as resp:
            pypi_data = json.loads(resp.read())
    except urllib.error.URLError as e:
        raise RuntimeError(f"Could not reach PyPI: {e}") from e

    version = pypi_data["info"]["version"]

    matching = [
        u for u in pypi_data["urls"]
        if u["filename"].endswith(".whl") and tag in u["filename"]
    ]
    if not matching:
        available = [u["filename"] for u in pypi_data["urls"] if u["filename"].endswith(".whl")]
        raise RuntimeError(
            f"No wheel for platform '{tag}' in caddytail {version}. "
            f"Available: {available}"
        )

    wheel_url = matching[0]["url"]
    wheel_size = matching[0]["size"]
    print(f"Downloading caddytail {version} ({wheel_size // 1024} KB)...")

    try:
        with urllib.request.urlopen(wheel_url) as resp:
            wheel_bytes = resp.read()
    except urllib.error.URLError as e:
        raise RuntimeError(f"Download failed: {e}") from e

    wheel_binary_path = f"caddytail/bin/{binary_name}"
    with zipfile.ZipFile(BytesIO(wheel_bytes)) as zf:
        if wheel_binary_path not in zf.namelist():
            raise RuntimeError(f"Binary '{wheel_binary_path}' not found in wheel")
        data = zf.read(wheel_binary_path)

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)

    if platform.system() != "Windows":
        dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    print(f"Downloaded caddy to {dest} ({len(data) // 1024} KB)")
    return str(dest)


def get_binary_path() -> str:
    """Get the path to the caddy binary.

    Checks in order: bundled binary, system caddy on PATH, then
    auto-downloads from CI on first use.
    """
    import shutil

    bundled = _bundled_binary_path()
    if os.path.exists(bundled):
        return bundled
    system = shutil.which("caddy")
    if system:
        return system
    try:
        return fetch_binary()
    except RuntimeError as e:
        print(f"Auto-fetch failed: {e}", file=sys.stderr)
        return bundled


# ---------------------------------------------------------------------------
# CLI routing
# ---------------------------------------------------------------------------

def _parse_env_args(env_list: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in env_list:
        if "=" not in item:
            print(f"Error: invalid --env value (expected KEY=VALUE): {item}", file=sys.stderr)
            sys.exit(1)
        key, _, value = item.partition("=")
        result[key] = value
    return result


def _cmd_run(args: list[str]) -> int:
    """caddytail run <hostname> <app_ref> [--debug]"""
    debug = "--debug" in args
    positional = [a for a in args if not a.startswith("--")]

    if len(positional) < 2:
        print("usage: caddytail run <hostname> <app_ref> [--debug]", file=sys.stderr)
        return 1

    hostname, app_ref = positional[0], positional[1]

    from .runner import run
    run(hostname, app_ref, debug=debug)
    return 0


def _cmd_install(args: list[str]) -> int:
    """caddytail install <hostname> <app_ref> [--no-start] [--env K=V]"""
    no_start = "--no-start" in args

    env_values: list[str] = []
    positional: list[str] = []
    i = 0
    while i < len(args):
        if args[i] == "--env" and i + 1 < len(args):
            env_values.append(args[i + 1])
            i += 2
        elif args[i].startswith("--"):
            i += 1
        else:
            positional.append(args[i])
            i += 1

    if len(positional) < 2:
        print("usage: caddytail install <hostname> <app_ref> [--no-start] [--env K=V]", file=sys.stderr)
        return 1

    hostname, app_ref = positional[0], positional[1]
    environment = _parse_env_args(env_values) if env_values else None

    from .systemd import install_service
    try:
        install_service(
            hostname,
            app_ref,
            environment=environment,
            start=not no_start,
        )
    except (RuntimeError, FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


def _cmd_uninstall(args: list[str]) -> int:
    """caddytail uninstall <hostname>"""
    if not args:
        print("usage: caddytail uninstall <hostname>", file=sys.stderr)
        return 1

    from .systemd import uninstall_service
    try:
        uninstall_service(args[0])
    except (RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


def _cmd_status(args: list[str]) -> int:
    """caddytail status <hostname>"""
    if not args:
        print("usage: caddytail status <hostname>", file=sys.stderr)
        return 1

    from .systemd import service_status, _print_status
    try:
        info = service_status(args[0])
        _print_status(info)
    except (RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


def _cmd_logs(args: list[str]) -> int:
    """caddytail logs <hostname> [-n LINES] [-f]"""
    follow = "-f" in args or "--follow" in args
    lines = 50

    positional: list[str] = []
    i = 0
    while i < len(args):
        if args[i] in ("-n", "--lines") and i + 1 < len(args):
            try:
                lines = int(args[i + 1])
            except ValueError:
                print(f"Error: invalid line count: {args[i + 1]}", file=sys.stderr)
                return 1
            i += 2
        elif args[i] in ("-f", "--follow"):
            i += 1
        else:
            positional.append(args[i])
            i += 1

    if not positional:
        print("usage: caddytail logs <hostname> [-n LINES] [-f]", file=sys.stderr)
        return 1

    from .systemd import service_logs
    try:
        service_logs(positional[0], lines=lines, follow=follow)
    except (RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


def _cmd_restart(args: list[str]) -> int:
    """caddytail restart <hostname>"""
    if not args:
        print("usage: caddytail restart <hostname>", file=sys.stderr)
        return 1

    from .systemd import restart_service
    try:
        restart_service(args[0])
    except (RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


def _cmd_list(args: list[str]) -> int:
    """caddytail list"""
    from .systemd import list_services, _print_status
    try:
        services = list_services()
        if not services:
            print("No caddytail services installed.")
        else:
            for info in services:
                _print_status(info)
                print()
    except (RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


def _cmd_caddy(args: list[str]) -> int:
    """caddytail caddy [args...] — raw Caddy pass-through"""
    binary = get_binary_path()

    if not os.path.exists(binary):
        print(f"Error: Caddy binary not found at {binary}", file=sys.stderr)
        return 1

    if sys.platform != "win32":
        os.chmod(binary, 0o755)

    return subprocess.call([binary] + args)


def main() -> int:
    """CLI entry point."""
    if len(sys.argv) < 2:
        print(USAGE)
        return 1

    cmd = sys.argv[1]
    args = sys.argv[2:]

    commands = {
        "run": _cmd_run,
        "install": _cmd_install,
        "uninstall": _cmd_uninstall,
        "status": _cmd_status,
        "logs": _cmd_logs,
        "restart": _cmd_restart,
        "list": _cmd_list,
        "caddy": _cmd_caddy,
    }

    handler = commands.get(cmd)
    if handler is None:
        print(f"Unknown command: {cmd}\n")
        print(USAGE)
        return 1

    return handler(args)


# ---------------------------------------------------------------------------
# Public API re-exports
# ---------------------------------------------------------------------------

from .api import (
    CaddyTail,
    StaticPath,
    TailscaleUser,
    get_user,
    get_user_or_error,
    login_required,
    static,
    get_tailnet_from_tailscale,
    # Legacy
    flask_user_required,
    fastapi_user_dependency,
)
from .systemd import (
    install_service,
    uninstall_service,
    service_status,
    restart_service,
    service_logs,
    list_services,
)

__all__ = [
    "__version__",
    "get_binary_path",
    "fetch_binary",
    "main",
    # New standalone API
    "get_user",
    "get_user_or_error",
    "login_required",
    "static",
    # Types
    "CaddyTail",
    "StaticPath",
    "TailscaleUser",
    # Utilities
    "get_tailnet_from_tailscale",
    # Legacy
    "flask_user_required",
    "fastapi_user_dependency",
    # systemd
    "install_service",
    "uninstall_service",
    "service_status",
    "restart_service",
    "service_logs",
    "list_services",
]


if __name__ == "__main__":
    sys.exit(main())
