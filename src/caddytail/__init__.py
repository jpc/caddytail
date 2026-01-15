"""Caddy web server with Tailscale plugin, packaged for pip installation."""

import os
import subprocess
import sys

__version__ = "0.1.0"


def get_binary_path() -> str:
    """Get the path to the caddy binary."""
    package_dir = os.path.dirname(__file__)
    binary_name = "caddy.exe" if sys.platform == "win32" else "caddy"
    return os.path.join(package_dir, "bin", binary_name)


def main() -> int:
    """Run the caddy binary with the provided arguments."""
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


if __name__ == "__main__":
    sys.exit(main())
