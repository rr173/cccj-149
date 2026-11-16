#!/usr/bin/env python3 -I
"""Isolated-mode launcher so the CLI runs without ambient site packages.

Usage: scripts/cli.sh <args...>  (this file is invoked by it), or directly:
    python3 -I scripts/run_cli.py read pkg1 --device dev1
"""
import os
import sys

sys.path.insert(0, os.path.expanduser("~/.local/lib/python3.11/site-packages"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from usage_intervals.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
