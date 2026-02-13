"""Caddy web server with Tailscale plugin, packaged for pip installation."""

import os
import subprocess
import sys

__version__ = "0.1.0"

_PYPI_URL = "https://pypi.org/pypi/caddytail/json"


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

    # Detect platform
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

    # Query PyPI for the latest release
    try:
        with urllib.request.urlopen(_PYPI_URL) as resp:
            pypi_data = json.loads(resp.read())
    except urllib.error.URLError as e:
        raise RuntimeError(f"Could not reach PyPI: {e}") from e

    version = pypi_data["info"]["version"]

    # Find the wheel matching our platform
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

    # Extract caddy binary from the wheel (which is a zip)
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
        return bundled  # fall through so the caller gets the normal "not found" error


def main() -> int:
    """Run the caddy binary with the provided arguments."""
    # Intercept 'service' subcommand for systemd management
    if len(sys.argv) > 1 and sys.argv[1] == "service":
        from .systemd import cli_main
        sys.argv = [sys.argv[0] + " service"] + sys.argv[2:]
        return cli_main()

    binary = get_binary_path()

    if not os.path.exists(binary):
        print(f"Error: Caddy binary not found at {binary}", file=sys.stderr)
        print("This may indicate a packaging issue or unsupported platform.", file=sys.stderr)
        return 1

    # Ensure the binary is executable on Unix-like systems
    if sys.platform != "win32":
        os.chmod(binary, 0o755)

    # Execute caddy with all arguments passed through
    return subprocess.call([binary] + sys.argv[1:])

from .api import (
    CaddyTail,
    StaticPath,
    TailscaleUser,
    flask_user_required,
    fastapi_user_dependency,
    get_tailnet_from_tailscale,
)
from .systemd import (
    install_service,
    uninstall_service,
    service_status,
    restart_service,
    service_logs,
    list_services,
)

# Backwards compatibility alias
TailscaleCaddy = CaddyTail

__all__ = [
    "__version__",
    "get_binary_path",
    "fetch_binary",
    "main",
    "CaddyTail",
    "TailscaleCaddy",  # Backwards compatibility
    "StaticPath",
    "TailscaleUser",
    "flask_user_required",
    "fastapi_user_dependency",
    "get_tailnet_from_tailscale",
    "install_service",
    "uninstall_service",
    "service_status",
    "restart_service",
    "service_logs",
    "list_services",
]


if __name__ == "__main__":
    sys.exit(main())
