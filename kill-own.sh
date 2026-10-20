#!/bin/bash
# Kill only the processes matching a pattern whose working directory is inside your own worktree, never
# another session's or your own runs elsewhere (pkill -f vitest would). The caller's shell is excluded.
# Usage: kill-own.sh <pgrep -f pattern> [worktree, default: git toplevel of $PWD]
set -uo pipefail

pattern=${1:?usage: kill-own.sh <pattern> [worktree]}
root=${2:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}
root=$(readlink -f "$root")
killed=0

for pid in $(pgrep -f -- "$pattern"); do
  if [ "$pid" = "$$" ] || [ "$pid" = "$PPID" ]; then
    continue
  fi

  cwd=$(readlink -f "/proc/$pid/cwd" 2>/dev/null) || continue

  case "$cwd/" in
    "$root"/*)
      description=$(tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null | cut -c1-80)

      if kill "$pid" 2>/dev/null; then
        killed=$((killed + 1))
        echo "killed $pid ($description)"
      fi
      ;;
  esac
done

echo "kill-own: $killed process(es) under $root"
