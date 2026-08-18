#!/bin/sh
#
# One working directory per branch, so several sessions can run at once without
# checking out over each other.
#
#   tool/worktree.sh new payment-retry feature
#   tool/worktree.sh list
#   tool/worktree.sh done payment-retry
#
# Worktrees are created as siblings of the repository, never inside it. A copy
# of the project within the project gets picked up by file watchers and language
# servers, producing duplicate-definition errors and a much slower analyse.
#
# They share .git, so `core.hooksPath` carries over — a new worktree has working
# hooks with no setup.

set -e

cd "$(dirname "$0")/.." || exit 1
root=$(pwd)
wt_root="$(dirname "$root")/$(basename "$root")-wt"
integration=${INTEGRATION_BRANCH:-dev}

usage() {
  echo "usage: tool/worktree.sh new <name> [feature|fix]"
  echo "       tool/worktree.sh list"
  echo "       tool/worktree.sh done <name>"
  exit 1
}

case "${1:-}" in
  new)
    name=${2:?"name required"}
    kind=${3:-feature}
    case "$kind" in feature|fix) ;; *) echo "kind must be feature or fix" >&2; exit 1 ;; esac

    git fetch -q origin "$integration"
    git worktree add -b "$kind/$name" "$wt_root/$kind-$name" "origin/$integration"

    # Each worktree gets its own dependency directory, so they have to be
    # resolved per directory. Doing it here means the session can start working
    # rather than discovering it on the first build.
    if [ -x tool/checks/install ]; then
      echo "→ install"
      (cd "$wt_root/$kind-$name" && ../../"$(basename "$root")"/tool/checks/install 2>/dev/null \
        || tool/checks/install)
    fi

    echo
    echo "Worktree ready:"
    echo "  cd $wt_root/$kind-$name"
    echo "  branch $kind/$name (from origin/$integration)"
    ;;

  list)
    echo "Worktrees in play:"
    git worktree list
    echo
    echo "Shared resources do not parallelise: a connected device, a running"
    echo "instance of the app, anything writing to one fixed path. Only one"
    echo "session at a time may hold those."
    ;;

  done)
    name=${2:?"name required"}
    # Match on the directory, which is what `git worktree remove` takes, and
    # which is named <kind>-<name> inside the -wt folder. sed rather than awk so
    # a path containing spaces survives.
    #
    # This is the command that had never been run before it was needed — see
    # decisions/006-verify-the-clone.md
    dir=$(git worktree list --porcelain \
          | sed -n 's/^worktree //p' \
          | grep -E "/(feature|fix)-${name}\$" \
          | head -1)
    [ -n "$dir" ] || { echo "no worktree matching '$name'" >&2; exit 1; }

    git worktree remove "$dir"
    echo "Removed $dir"
    echo "The branch is kept; delete it once its pull request has merged."
    ;;

  *) usage ;;
esac
