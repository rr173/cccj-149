#!/usr/bin/env bash
# User-space PostgreSQL setup for environments without root / a package manager.
# Downloads the Debian PostgreSQL 15 debs, extracts them under $HOME/pg,
# initializes a cluster on port 55432 and creates the usage_dev database.
set -euo pipefail

PORT="${UI_PORT:-55432}"
SOCKDIR="${UI_SOCKDIR:-/tmp}"
PGROOT="$HOME/pg"
PGDATA="$HOME/pgdata"
PGBIN="$PGROOT/usr/lib/postgresql/15/bin"
LIBPQ="$PGROOT/usr/lib/aarch64-linux-gnu"
DEBDIR="$(mktemp -d)"
trap 'rm -rf "$DEBDIR"' EXIT

if [ ! -x "$PGBIN/postgres" ]; then
  echo ">> downloading PostgreSQL 15 (arm64) debs"
  base="http://deb.debian.org/debian/pool/main/p/postgresql-15"
  ver=$(curl -s "$base/" | grep -o 'postgresql-15_[^"]*_arm64.deb' | sort -u | tail -1)
  ( cd "$DEBDIR"
    curl -sSLO "$base/$ver"
    cver=$(curl -s "$base/" | grep -o 'postgresql-client-15_[^"]*_arm64.deb' | sort -u | tail -1)
    curl -sSLO "$base/$cver" )
  mkdir -p "$PGROOT"
  for d in "$DEBDIR"/*.deb; do dpkg-deb -x "$d" "$PGROOT"; done
fi

export LD_LIBRARY_PATH="$LIBPQ${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

if [ ! -d "$PGDATA/base" ]; then
  echo ">> initializing cluster at $PGDATA"
  "$PGBIN/initdb" -D "$PGDATA" -U postgres --auth=trust --encoding=UTF8 >/dev/null
fi

if ! "$PGBIN/pg_isready" -p "$PORT" -h "$SOCKDIR" >/dev/null 2>&1; then
  echo ">> starting server on port $PORT"
  "$PGBIN/pg_ctl" -D "$PGDATA" -l "$PGDATA/server.log" \
      -o "-p $PORT -k $SOCKDIR" start
  for _ in $(seq 1 30); do
    "$PGBIN/pg_isready" -p "$PORT" -h "$SOCKDIR" >/dev/null 2>&1 && break
    sleep 1
  done
fi

"$PGBIN/psql" -p "$PORT" -h "$SOCKDIR" -U postgres -tc \
  "SELECT 1 FROM pg_database WHERE datname='usage_dev'" | grep -q 1 \
  || "$PGBIN/createdb" -p "$PORT" -h "$SOCKDIR" -U postgres usage_dev

echo ">> ready: host=$SOCKDIR port=$PORT db=usage_dev user=postgres"
