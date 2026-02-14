"""
systemd user service management for caddytail applications.

CLI usage:
    caddytail install <hostname> <app_ref> [--no-start] [--env K=V]
    caddytail uninstall <hostname>
    caddytail status <hostname>
    caddytail restart <hostname>
    caddytail logs <hostname> [-n LINES] [-f]
    caddytail list
"""

from __future__ import annotations

import os
import re
import shutil
import signal
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
ExecStart={caddytail_path} run {hostname} {app_ref}
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
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        base = Path(xdg)
    else:
        base = Path.home() / ".config"
    return base / "systemd" / "user"


def _default_service_name(hostname: str) -> str:
    sanitized = re.sub(r"[^a-zA-Z0-9_-]", "-", hostname)
    return f"{SERVICE_PREFIX}{sanitized}"


def _unit_path(service_name: str) -> Path:
    return _get_user_systemd_dir() / f"{service_name}.service"


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    cmd = ["systemctl", "--user", *args]
    return subprocess.run(cmd, capture_output=True, text=True)


def _resolve_service_name(hostname: str) -> str:
    return _default_service_name(hostname)


def _find_caddytail_path() -> str:
    """Find the path to the caddytail CLI binary."""
    ct = shutil.which("caddytail")
    if ct:
        return ct
    # Fallback: use python -m caddytail
    return f"{sys.executable} -m caddytail"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def install_service(
    hostname: str,
    app_ref: str,
    *,
    working_directory: Optional[str] = None,
    environment: Optional[dict[str, str]] = None,
    enable: bool = True,
    start: bool = True,
) -> Path:
    """
    Install a caddytail app as a systemd user service.

    ExecStart runs ``caddytail run <hostname> <app_ref>``.

    Args:
        hostname: Tailscale hostname (also used to derive service name).
        app_ref: App reference in module:variable form (e.g. "app:app").
        working_directory: Working directory for the service (default: cwd).
        environment: Extra environment variables.
        enable: Enable the service so it starts on login.
        start: Start the service immediately.

    Returns:
        Path to the written unit file.
    """
    _require_linux()
    _require_systemctl()

    svc = _default_service_name(hostname)
    workdir = str(Path(working_directory).resolve()) if working_directory else os.getcwd()
    caddytail_path = _find_caddytail_path()

    env_lines = ""
    if environment:
        for key, value in environment.items():
            env_lines += f'Environment="{key}={value}"\n'

    unit_content = UNIT_TEMPLATE.format(
        hostname=hostname,
        app_ref=app_ref,
        working_directory=workdir,
        caddytail_path=caddytail_path,
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

    print()
    print("Hint: to keep the service running after you log out, run:")
    print(f"  loginctl enable-linger {os.environ.get('USER', '$USER')}")

    # Auto-tail logs if running in a terminal
    if start and sys.stdout.isatty():
        print()
        print("Tailing logs (Ctrl-C to detach, service keeps running)...")
        print()
        _tail_logs_until_ctrl_c(svc)

    return unit_file


def uninstall_service(hostname: str) -> None:
    """Stop, disable, and remove a caddytail systemd user service."""
    _require_linux()
    _require_systemctl()

    svc = _resolve_service_name(hostname)
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


def service_status(hostname: str) -> dict[str, Any]:
    """Return status information for a caddytail service."""
    _require_linux()
    _require_systemctl()

    svc = _resolve_service_name(hostname)
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


def restart_service(hostname: str) -> None:
    """Restart a caddytail systemd user service."""
    _require_linux()
    _require_systemctl()

    svc = _resolve_service_name(hostname)
    result = _systemctl("restart", f"{svc}.service")
    if result.returncode == 0:
        print(f"Service restarted: {svc}")
    else:
        print(f"Failed to restart service: {result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)


def service_logs(
    hostname: str,
    *,
    lines: int = 50,
    follow: bool = False,
) -> None:
    """Show journal logs for a caddytail service."""
    _require_linux()

    svc = _resolve_service_name(hostname)
    cmd = ["journalctl", "--user-unit", f"{svc}.service", "-n", str(lines)]
    if follow:
        cmd.append("-f")

    subprocess.run(cmd)


def list_services() -> list[dict[str, Any]]:
    """List all installed caddytail systemd user services."""
    _require_linux()
    _require_systemctl()

    unit_dir = _get_user_systemd_dir()
    services: list[dict[str, Any]] = []
    if not unit_dir.is_dir():
        return services

    for path in sorted(unit_dir.glob(f"{SERVICE_PREFIX}*.service")):
        svc = path.stem
        # Extract hostname from service name
        hostname = svc[len(SERVICE_PREFIX):]
        info = service_status(hostname)
        services.append(info)
    return services


# ---------------------------------------------------------------------------
# Log tailing with clean Ctrl-C detach
# ---------------------------------------------------------------------------

def _tail_logs_until_ctrl_c(service_name: str) -> None:
    """Tail journalctl logs. Ctrl-C stops tailing but keeps the service running."""
    cmd = ["journalctl", "--user-unit", f"{service_name}.service", "-f", "-n", "20"]

    proc = subprocess.Popen(cmd)

    # Ctrl-C handler: kill the journalctl process, don't propagate
    original_sigint = signal.getsignal(signal.SIGINT)

    def detach_handler(signum, frame):
        proc.terminate()

    signal.signal(signal.SIGINT, detach_handler)

    try:
        proc.wait()
    finally:
        signal.signal(signal.SIGINT, original_sigint)

    print("\nDetached from logs. Service is still running.")
    print(f"  caddytail logs {service_name[len(SERVICE_PREFIX):]}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

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
