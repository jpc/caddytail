#!/usr/bin/env python3
"""Download the caddy binary from the latest PyPI release.

Thin wrapper around ``caddytail.fetch_binary()``.
Normally this happens automatically on first use, but you can run this
script explicitly to pre-fetch the binary.
"""

import sys
from caddytail import fetch_binary

try:
    path = fetch_binary()
except RuntimeError as e:
    print(f"Error: {e}", file=sys.stderr)
    sys.exit(1)

print(f"Binary ready at {path}")
