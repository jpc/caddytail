"""
Watchdog process for Caddy.

This module implements a watchdog that monitors the main application process
via a pipe. When the pipe is closed (indicating the main process has died),
the watchdog shuts down Caddy.

The watchdog runs as a separate process spawned by the main application.
"""

import os
import signal
import sys
import time
import urllib.request
import urllib.error


def shutdown_caddy(admin_port: int, caddy_pid: int) -> None:
    """Attempt to shut down Caddy gracefully, then forcefully if needed."""
    # Try graceful shutdown via admin API
    try:
        url = f"http://localhost:{admin_port}/stop"
        req = urllib.request.Request(url, data=b"", method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=5.0):
            pass
        print(f"[watchdog] Caddy stopped via admin API", file=sys.stderr)
        return
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
        print(f"[watchdog] Admin API shutdown failed: {e}", file=sys.stderr)

    # Fall back to sending SIGTERM to Caddy process
    if caddy_pid > 0:
        try:
            os.kill(caddy_pid, signal.SIGTERM)
            print(f"[watchdog] Sent SIGTERM to Caddy (PID {caddy_pid})", file=sys.stderr)
            # Give it a moment to shut down
            time.sleep(2)
            # Check if still running, send SIGKILL if needed
            try:
                os.kill(caddy_pid, 0)  # Check if process exists
                os.kill(caddy_pid, signal.SIGKILL)
                print(f"[watchdog] Sent SIGKILL to Caddy (PID {caddy_pid})", file=sys.stderr)
            except OSError:
                pass  # Process already gone
        except OSError as e:
            print(f"[watchdog] Failed to kill Caddy: {e}", file=sys.stderr)


def watchdog_main(read_fd: int, admin_port: int, caddy_pid: int) -> None:
    """
    Main watchdog loop.

    Monitors the read end of a pipe. When the pipe is closed (EOF),
    it means the main process has exited, and we should shut down Caddy.

    Args:
        read_fd: File descriptor for the read end of the pipe
        admin_port: Caddy admin API port
        caddy_pid: Caddy process PID
    """
    print(f"[watchdog] Started (PID {os.getpid()}), monitoring main process", file=sys.stderr)
    print(f"[watchdog] Caddy PID: {caddy_pid}, Admin port: {admin_port}", file=sys.stderr)

    # Open the file descriptor as a file object
    try:
        pipe_read = os.fdopen(read_fd, 'rb')
    except OSError as e:
        print(f"[watchdog] Failed to open pipe fd {read_fd}: {e}", file=sys.stderr)
        shutdown_caddy(admin_port, caddy_pid)
        sys.exit(1)

    try:
        # Block reading from the pipe - this will return empty when the write end is closed
        while True:
            data = pipe_read.read(1)
            if not data:
                # Pipe closed - main process died
                print("[watchdog] Pipe closed, main process exited", file=sys.stderr)
                break
    except OSError as e:
        print(f"[watchdog] Pipe read error: {e}", file=sys.stderr)
    finally:
        pipe_read.close()

    # Shut down Caddy
    shutdown_caddy(admin_port, caddy_pid)
    print("[watchdog] Exiting", file=sys.stderr)
    sys.exit(0)


if __name__ == "__main__":
    # When run as a script, expect arguments: read_fd admin_port caddy_pid
    if len(sys.argv) != 4:
        print(f"Usage: {sys.argv[0]} <read_fd> <admin_port> <caddy_pid>", file=sys.stderr)
        sys.exit(1)

    read_fd = int(sys.argv[1])
    admin_port = int(sys.argv[2])
    caddy_pid = int(sys.argv[3])

    watchdog_main(read_fd, admin_port, caddy_pid)
