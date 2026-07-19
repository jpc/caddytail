"""Custom build step: compile the caddy binary from the local caddy-tailscale fork.

This runs automatically during ``pip install -e .`` (editable installs) and
regular ``pip install .`` when the ``caddy-tailscale/`` directory is present.
CI builds use xcaddy separately, so this is primarily for local development.

Requirements: Go toolchain (``go`` on PATH).
"""

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py
from setuptools.command.develop import develop
from setuptools.command.editable_wheel import editable_wheel

CADDY_FORK_DIR = Path(__file__).parent / "caddy-tailscale"
BINARY_NAME = "caddy.exe" if sys.platform == "win32" else "caddy"
BINARY_DEST = Path(__file__).parent / "src" / "caddytail" / "bin" / BINARY_NAME


def _needs_build():
    """Check whether we should build the caddy binary."""
    if not CADDY_FORK_DIR.is_dir():
        return False
    if not shutil.which("go"):
        print(
            "WARNING: caddy-tailscale/ found but Go is not installed. "
            "Skipping caddy binary build.",
            file=sys.stderr,
        )
        return False
    return True


def _build_caddy():
    """Build the caddy binary from the local caddy-tailscale fork."""
    if not _needs_build():
        return

    print(f"Building caddy binary from {CADDY_FORK_DIR}...")
    BINARY_DEST.parent.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["CGO_ENABLED"] = "0"

    try:
        subprocess.check_call(
            [
                "go", "build",
                "-o", str(BINARY_DEST),
                "./cmd/caddy",
            ],
            cwd=str(CADDY_FORK_DIR),
            env=env,
        )
    except subprocess.CalledProcessError as e:
        print(f"ERROR: caddy build failed (exit {e.returncode})", file=sys.stderr)
        raise SystemExit(1)

    # Ensure executable
    if platform.system() != "Windows":
        BINARY_DEST.chmod(BINARY_DEST.stat().st_mode | 0o111)

    size_mb = BINARY_DEST.stat().st_size / (1024 * 1024)
    print(f"Built caddy binary: {BINARY_DEST} ({size_mb:.1f} MB)")


class BuildPyWithCaddy(build_py):
    def run(self):
        _build_caddy()
        super().run()


class DevelopWithCaddy(develop):
    def run(self):
        _build_caddy()
        super().run()


class EditableWheelWithCaddy(editable_wheel):
    def run(self):
        _build_caddy()
        super().run()


setup(
    cmdclass={
        "build_py": BuildPyWithCaddy,
        "develop": DevelopWithCaddy,
        "editable_wheel": EditableWheelWithCaddy,
    },
)
