#!/bin/sh
#
# Run this first, every session. It checks the things that fail later in
# confusing ways if they are wrong now.
#
# The hooks check is a hard failure rather than a warning, and that is the
# reason this script exists at all: a fresh clone has no hooks until
# core.hooksPath is set, so the first commit from a new checkout silently
# skips formatting, linting and the secret scan. A warning would be read once
# and then scrolled past.
#
# Everything else here is a check whose absence produces a bad error message
# somewhere far from the cause — a SQLite too old to run the job claim, or a
# database on a filesystem that cannot do WAL locking.

set -e

failures=0

ok()   { printf '\033[32m✓\033[0m %s\n' "$1"; }
warn() { printf '· %s\n' "$1"; }
bad()  { printf '\033[31m✗ %s\033[0m\n' "$1" >&2; failures=$((failures + 1)); }

# --- git hooks: the one that cannot be a warning -------------------------
hooks_path=$(git config core.hooksPath 2>/dev/null || true)
if [ "$hooks_path" = ".githooks" ]; then
  ok "git hooks enabled (core.hooksPath=.githooks)"
else
  bad "git hooks are NOT enabled. Run:  git config core.hooksPath .githooks"
fi

# --- toolchain -----------------------------------------------------------
if command -v uv >/dev/null 2>&1; then
  ok "uv $(uv --version 2>/dev/null | awk '{print $2}')"
else
  bad "uv is not installed. See https://docs.astral.sh/uv/"
fi

if command -v python3 >/dev/null 2>&1; then
  ok "python $(python3 --version 2>&1 | awk '{print $2}')"
else
  bad "python3 is not installed"
fi

# --- PostgreSQL: the only backend since the cutover (#15) -----------------
#
# There is no SQLite fallback any more (docs/status.md, settings.py): every
# command refuses outright with no TUBEDEPTH_DATABASE_URL, and refuses again
# if the server it names is not reachable. Both failures are confusing from
# inside a worker or a migration; this check exists to name them here first.
if [ -z "$TUBEDEPTH_DATABASE_URL" ]; then
  bad "TUBEDEPTH_DATABASE_URL is not set; tubedepth has no SQLite fallback (see AGENTS.md)"
else
  host_port=$(python3 -c "
import os
from urllib.parse import urlsplit
u = urlsplit(os.environ['TUBEDEPTH_DATABASE_URL'])
print(u.hostname or '', u.port or 5432)
" 2>/dev/null)
  host=$(echo "$host_port" | awk '{print $1}')
  port=$(echo "$host_port" | awk '{print $2}')
  if [ -z "$host" ]; then
    bad "TUBEDEPTH_DATABASE_URL did not parse as host:port"
  elif ! command -v pg_isready >/dev/null 2>&1; then
    warn "pg_isready not installed; cannot verify $host:$port is reachable"
  elif pg_isready -h "$host" -p "$port" -q 2>/dev/null; then
    ok "PostgreSQL reachable at $host:$port"
  else
    bad "PostgreSQL at $host:$port is not accepting connections"
  fi
fi

# --- The payload store wants a filesystem with reliable POSIX semantics ---
#
# On WSL, /mnt/c is drvfs. This stopped being about SQLite's WAL locking at
# the cutover (#15) — the database is PostgreSQL now, reached over TCP — but
# TUBEDEPTH_DATA_DIR/payloads is still local gzip files, and drvfs is still
# the filesystem that produced the intermittent "database is locked" this
# check used to be named for. See docs/troubleshooting.md's SQLite section,
# kept as history rather than deleted.
data_dir="${TUBEDEPTH_DATA_DIR:-./var}"
case "$(cd "$data_dir" 2>/dev/null && pwd || echo "$data_dir")" in
  /mnt/*) bad "TUBEDEPTH_DATA_DIR is under /mnt (drvfs); avoid it for the payload store" ;;
  *)      ok  "TUBEDEPTH_DATA_DIR is on a filesystem with reliable POSIX semantics" ;;
esac

# --- egress pool: optional until the pool is configured -------------------
if command -v wireproxy >/dev/null 2>&1; then
  ok "wireproxy $(wireproxy --version 2>/dev/null | head -1 || echo present)"
else
  warn "wireproxy not installed — the egress pool will run direct-only."
  warn "  install with: nix profile install nixpkgs#wireproxy"
fi

wireguard_dir="${TUBEDEPTH_EGRESS_WIREGUARD_DIR:-$HOME/.config/tubedepth/wireguard}"
if [ -d "$wireguard_dir" ]; then
  mode=$(stat -c '%a' "$wireguard_dir" 2>/dev/null || echo "???")
  if [ "$mode" = "700" ]; then
    ok "wireguard config directory is 0700"
  else
    bad "$wireguard_dir is mode $mode; it holds private keys and must be 0700"
  fi
fi

echo
if [ "$failures" -gt 0 ]; then
  printf '\033[31m✗ %s check(s) failed.\033[0m\n' "$failures" >&2
  exit 1
fi
printf '\033[32m✓ ready\033[0m\n'
