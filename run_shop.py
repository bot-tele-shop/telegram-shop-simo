"""Absolute-path friendly launcher; equivalent to python -m shop."""
from shop.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
