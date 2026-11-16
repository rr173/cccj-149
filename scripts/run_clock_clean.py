#!/usr/bin/env python3 -I
"""Isolated-mode launcher for the offline clock-cleaning CLI.

Usage: scripts/clock_clean.sh import manifest.json -o out/
       scripts/clock_clean.sh explain manifest.json -r rec-id
"""
import os
import sys

sys.path.insert(0, os.path.expanduser("~/.local/lib/python3.11/site-packages"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from usage_intervals.clock_cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
