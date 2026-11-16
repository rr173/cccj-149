#!/usr/bin/env bash
# Convenience wrapper: isolates Python path and points at the local cluster.
set -euo pipefail
export UI_DSN="${UI_DSN:-host=/tmp port=55432 user=postgres dbname=usage_dev}"
exec python3 -I "$(dirname "$0")/run_cli.py" "$@"
