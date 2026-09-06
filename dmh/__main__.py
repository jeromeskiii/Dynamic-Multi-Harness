"""Main CLI entrypoint when invoked as `python3 -m dmh`."""

import sys
from dmh.cli import main

if __name__ == "__main__":
    sys.exit(main())
