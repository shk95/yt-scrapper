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

# --- SQLite: the job claim depends on the version -------------------------
#
# BEGIN IMMEDIATE is ancient, but UPDATE ... RETURNING needs 3.35 and the
# repositories use it. Discovering that at claim time means an OperationalError
# inside a worker, which is the worst possible place to learn about it.
sqlite_version=$(python3 -c 'import sqlite3; print(sqlite3.sqlite_version)' 2>/dev/null || echo "0.0.0")
sqlite_major=$(echo "$sqlite_version" | cut -d. -f1)
sqlite_minor=$(echo "$sqlite_version" | cut -d. -f2)
if [ "$sqlite_major" -gt 3 ] 2>/dev/null || { [ "$sqlite_major" -eq 3 ] && [ "$sqlite_minor" -ge 35 ]; } 2>/dev/null; then
  ok "sqlite $sqlite_version (>= 3.35, RETURNING available)"
else
  bad "sqlite $sqlite_version is too old; the job claim needs >= 3.35 for RETURNING"
fi

# --- WAL needs real POSIX locking ----------------------------------------
#
# On WSL, /mnt/c is drvfs, which does not provide the locking WAL relies on.
# A database there produces intermittent "database is locked" under the
# concurrency this project is built around. See docs/troubleshooting.md.
database_dir=$(dirname "${TUBEDEPTH_DATABASE:-./var/tubedepth.db}")
case "$(cd "$database_dir" 2>/dev/null && pwd || echo "$database_dir")" in
  /mnt/*) bad "the database path is under /mnt (drvfs); WAL locking is unreliable there" ;;
  *)      ok  "database path is on a filesystem that supports WAL locking" ;;
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
