"""Windowless entry point: prints remain safe when Windows provides no stdio."""
import os
import sys

if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

from outlook_triage.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
