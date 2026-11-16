#!/usr/bin/env bash
# Offline local-clock cleaning entry point. No database connection is needed.
set -euo pipefail
exec python3 -I "$(dirname "$0")/run_clock_clean.py" "$@"
