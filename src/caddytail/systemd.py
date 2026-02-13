"""
systemd user service management for caddytail applications.

Provides functions to install, uninstall, and manage caddytail apps as
systemd user services. Works with both the CLI (`caddytail service ...`)
and the Python API (`caddy.install_service()`).
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional


UNIT_TEMPLATE = """\
[Unit]
Description=CaddyTail: {hostname}
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={working_directory}
ExecStart={python_path} {script_path}
Restart=on-failure
RestartSec=5
{environment_lines}

[Install]
WantedBy=default.target
"""

SERVICE_PREFIX = "caddytail-"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require_linux() -> None:
    if sys.platform != "linux":
        raise RuntimeError("systemd service management is only supported on Linux")


def _require_systemctl() -> None:
    if shutil.which("systemctl") is None:
        raise RuntimeError("systemctl not found — is systemd installed?")


def _get_user_systemd_dir() -> Path:
    """Return the systemd user unit directory, respecting XDG_CONFIG_HOME."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        base = Path(xdg)
    else:
        base = Path.home() / ".config"
    return base / "systemd" / "user"


def _default_service_name(hostname: str) -> str:
    """Sanitize a hostname into a valid systemd unit name."""
    sanitized = re.sub(r"[^a-zA-Z0-9_-]", "-", hostname)
    return f"{SERVICE_PREFIX}{sanitized}"


def _unit_path(service_name: str) -> Path:
    return _get_user_systemd_dir() / f"{service_name}.service"


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    """Run ``systemctl --user`` with the given arguments."""
    cmd = ["systemctl", "--user", *args]
    return subprocess.run(cmd, capture_output=True, text=True)


def _resolve_service_name(
    hostname: Optional[str] = None,
    name: Optional[str] = None,
) -> str:
    if name:
        return name
    if hostname:
        return _default_service_name(hostname)
    raise ValueError("Either 'hostname' or 'name' must be provided")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def install_service(
    script_path: str,
    hostname: str,
    *,
    service_name: Optional[str] = None,
    python_path: Optional[str] = None,
    working_directory: Optional[str] = None,
    environment: Optional[dict[str, str]] = None,
    enable: bool = True,
    start: bool = True,
) -> Path:
    """
    Install a caddytail app as a systemd user service.

    Args:
        script_path: Path to the Python script that runs the app.
        hostname: Tailscale hostname used to derive the service name.
        service_name: Override the auto-generated unit name.
        python_path: Python interpreter to use (default: current ``sys.executable``).
        working_directory: Working directory for the service (default: script's parent).
        environment: Extra environment variables to set in the unit.
        enable: Enable the service so it starts on login (default True).
        start: Start the service immediately after install (default True).

    Returns:
        Path to the written unit file.
    """
    _require_linux()
    _require_systemctl()

    resolved_script = Path(script_path).resolve()
    if not resolved_script.is_file():
        raise FileNotFoundError(f"Script not found: {script_path}")

    svc = service_name or _default_service_name(hostname)
    py = python_path or sys.executable
    workdir = str(Path(working_directory).resolve()) if working_directory else str(resolved_script.parent)

    # Build Environment= lines
    env_lines = ""
    if environment:
        for key, value in environment.items():
            env_lines += f'Environment="{key}={value}"\n'

    unit_content = UNIT_TEMPLATE.format(
        hostname=hostname,
        working_directory=workdir,
        python_path=py,
        script_path=resolved_script,
        environment_lines=env_lines.rstrip(),
    )

    unit_dir = _get_user_systemd_dir()
    unit_dir.mkdir(parents=True, exist_ok=True)

    unit_file = unit_dir / f"{svc}.service"
    if unit_file.exists():
        print(f"Overwriting existing unit file: {unit_file}")
    unit_file.write_text(unit_content)
    print(f"Unit file written: {unit_file}")

    _systemctl("daemon-reload")

    if enable:
        result = _systemctl("enable", f"{svc}.service")
        if result.returncode == 0:
            print(f"Service enabled: {svc}")
        else:
            print(f"Warning: failed to enable service: {result.stderr.strip()}")

    if start:
        result = _systemctl("start", f"{svc}.service")
        if result.returncode == 0:
            print(f"Service started: {svc}")
        else:
            print(f"Warning: failed to start service: {result.stderr.strip()}")

    # Linger hint
    print()
    print("Hint: to keep the service running after you log out, run:")
    print(f"  loginctl enable-linger {os.environ.get('USER', '$USER')}")

    return unit_file


def uninstall_service(
    *,
    hostname: Optional[str] = None,
    name: Optional[str] = None,
) -> None:
    """
    Stop, disable, and remove a caddytail systemd user service.

    Provide either *hostname* (to derive the unit name) or *name* (exact unit name).
    """
    _require_linux()
    _require_systemctl()

    svc = _resolve_service_name(hostname, name)
    unit = f"{svc}.service"

    _systemctl("stop", unit)
    _systemctl("disable", unit)

    unit_file = _unit_path(svc)
    if unit_file.exists():
        unit_file.unlink()
        print(f"Removed unit file: {unit_file}")
    else:
        print(f"Unit file not found (already removed?): {unit_file}")

    _systemctl("daemon-reload")
    _systemctl("reset-failed", unit)
    print(f"Service uninstalled: {svc}")


def service_status(
    *,
    hostname: Optional[str] = None,
    name: Optional[str] = None,
) -> dict[str, Any]:
    """
    Return status information for a caddytail service.

    Returns a dict with keys: active, enabled, pid, status, unit.
    """
    _require_linux()
    _require_systemctl()

    svc = _resolve_service_name(hostname, name)
    unit = f"{svc}.service"

    result = _systemctl(
        "show", unit,
        "--property=ActiveState,SubState,MainPID,UnitFileState,Description",
    )
    info: dict[str, Any] = {"unit": unit}
    for line in result.stdout.strip().splitlines():
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key == "ActiveState":
            info["active"] = value
        elif key == "SubState":
            info["status"] = value
        elif key == "MainPID":
            info["pid"] = int(value) if value.isdigit() else 0
        elif key == "UnitFileState":
            info["enabled"] = value
        elif key == "Description":
            info["description"] = value
    return info


def restart_service(
    *,
    hostname: Optional[str] = None,
    name: Optional[str] = None,
) -> None:
    """Restart a caddytail systemd user service."""
    _require_linux()
    _require_systemctl()

    svc = _resolve_service_name(hostname, name)
    result = _systemctl("restart", f"{svc}.service")
    if result.returncode == 0:
        print(f"Service restarted: {svc}")
    else:
        print(f"Failed to restart service: {result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)


def service_logs(
    *,
    hostname: Optional[str] = None,
    name: Optional[str] = None,
    lines: int = 50,
    follow: bool = False,
) -> None:
    """
    Show journal logs for a caddytail service.

    When *follow* is True this call blocks and streams output until interrupted.
    """
    _require_linux()

    svc = _resolve_service_name(hostname, name)
    cmd = ["journalctl", "--user-unit", f"{svc}.service", "-n", str(lines)]
    if follow:
        cmd.append("-f")

    # Stream directly to stdout so the user sees output in real time.
    subprocess.run(cmd)


def list_services() -> list[dict[str, Any]]:
    """
    List all installed caddytail systemd user services.

    Returns a list of dicts, each containing unit name and status info.
    """
    _require_linux()
    _require_systemctl()

    unit_dir = _get_user_systemd_dir()
    services: list[dict[str, Any]] = []
    if not unit_dir.is_dir():
        return services

    for path in sorted(unit_dir.glob(f"{SERVICE_PREFIX}*.service")):
        svc = path.stem  # e.g. "caddytail-myapp"
        info = service_status(name=svc)
        services.append(info)
    return services


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cli_main() -> int:
    """Entry point for ``caddytail service`` subcommands."""
    parser = argparse.ArgumentParser(
        prog="caddytail service",
        description="Manage caddytail systemd user services",
    )
    sub = parser.add_subparsers(dest="command")

    # -- install --------------------------------------------------------
    p_install = sub.add_parser("install", help="Install a caddytail app as a service")
    p_install.add_argument("script", help="Path to the Python app script")
    p_install.add_argument("--hostname", required=True, help="Tailscale hostname")
    p_install.add_argument("--name", default=None, help="Override service unit name")
    p_install.add_argument("--python", default=None, help="Python interpreter path")
    p_install.add_argument("--workdir", default=None, help="Working directory (default: script's parent)")
    p_install.add_argument(
        "--env", action="append", default=[], metavar="KEY=VALUE",
        help="Environment variable (can be repeated)",
    )
    p_install.add_argument(
        "--no-enable", action="store_true",
        help="Do not enable the service after install",
    )
    p_install.add_argument(
        "--no-start", action="store_true",
        help="Do not start the service after install",
    )

    # -- uninstall ------------------------------------------------------
    p_uninstall = sub.add_parser("uninstall", help="Uninstall a caddytail service")
    _add_service_id_args(p_uninstall)

    # -- status ---------------------------------------------------------
    p_status = sub.add_parser("status", help="Show service status")
    _add_service_id_args(p_status)

    # -- restart --------------------------------------------------------
    p_restart = sub.add_parser("restart", help="Restart a caddytail service")
    _add_service_id_args(p_restart)

    # -- logs -----------------------------------------------------------
    p_logs = sub.add_parser("logs", help="Show service logs")
    _add_service_id_args(p_logs)
    p_logs.add_argument("-n", "--lines", type=int, default=50, help="Number of lines")
    p_logs.add_argument("-f", "--follow", action="store_true", help="Follow log output")

    # -- list -----------------------------------------------------------
    sub.add_parser("list", help="List installed caddytail services")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        return 1

    try:
        if args.command == "install":
            env = _parse_env_args(args.env)
            install_service(
                script_path=args.script,
                hostname=args.hostname,
                service_name=args.name,
                python_path=args.python,
                working_directory=args.workdir,
                environment=env or None,
                enable=not args.no_enable,
                start=not args.no_start,
            )
        elif args.command == "uninstall":
            uninstall_service(hostname=args.hostname, name=args.name)
        elif args.command == "status":
            info = service_status(hostname=args.hostname, name=args.name)
            _print_status(info)
        elif args.command == "restart":
            restart_service(hostname=args.hostname, name=args.name)
        elif args.command == "logs":
            service_logs(
                hostname=args.hostname, name=args.name,
                lines=args.lines, follow=args.follow,
            )
        elif args.command == "list":
            services = list_services()
            if not services:
                print("No caddytail services installed.")
            else:
                for info in services:
                    _print_status(info)
                    print()
    except (RuntimeError, FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 0


def _add_service_id_args(parser: argparse.ArgumentParser) -> None:
    """Add mutually-exclusive --hostname / --name arguments."""
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--hostname", default=None, help="Tailscale hostname")
    group.add_argument("--name", default=None, help="Exact service unit name")


def _parse_env_args(env_list: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in env_list:
        if "=" not in item:
            raise ValueError(f"Invalid --env value (expected KEY=VALUE): {item}")
        key, _, value = item.partition("=")
        result[key] = value
    return result


def _print_status(info: dict[str, Any]) -> None:
    unit = info.get("unit", "?")
    active = info.get("active", "unknown")
    status = info.get("status", "unknown")
    enabled = info.get("enabled", "unknown")
    pid = info.get("pid", 0)
    desc = info.get("description", "")

    print(f"  Unit:    {unit}")
    if desc:
        print(f"  Desc:    {desc}")
    print(f"  Active:  {active} ({status})")
    print(f"  Enabled: {enabled}")
    if pid:
        print(f"  PID:     {pid}")
