"""Allow running as `python -m caddytail`."""

import sys

from caddytail import main

if __name__ == "__main__":
    sys.exit(main())
