#!/usr/bin/env bash
# .git/hooks/post-commit — installed by auto-routines.
#
# This script is generated, not handwritten. The auto-routines skill writes
# this file at install time and updates it on every `evolve` that changes
# git-hook routines.
#
# What it does:
#   1. Reads the list of git-hook routines from .iteration/config.yaml
#   2. For each one whose state is ACTIVE or EVOLVING, invokes
#      `claude -p "/<routine_id>"` non-interactively, in the background,
#      so the commit returns immediately.
#   3. Writes outcome lines to .iteration/log.jsonl.
#
# This hook MUST be idempotent and non-blocking. Never `exit 1` — it would
# block the user's commit. Worst case is a logged error.

set -u

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)"
[ -z "$REPO_ROOT" ] && exit 0

CONFIG="$REPO_ROOT/.iteration/config.yaml"
LOG="$REPO_ROOT/.iteration/log.jsonl"
HOOK_LOG="$REPO_ROOT/.iteration/hook-output.log"

[ -f "$CONFIG" ] || exit 0
mkdir -p "$REPO_ROOT/.iteration"
touch "$HOOK_LOG"

# Trap any error so we never block a commit.
trap 'echo "{\"ts\":\"$(date +%Y-%m-%dT%H:%M:%S%z)\",\"hook\":\"post-commit\",\"outcome\":\"err\",\"summary\":\"hook failed at line $LINENO\"}" >> "$LOG" 2>/dev/null; exit 0' ERR

# {{routine_dispatch_block}}
#
# At install time auto-routines replaces the line above with one block per
# active git-hook routine, like:
#
#   ( claude --dangerously-skip-permissions -p "/commit-tests" \
#       >> "$HOOK_LOG" 2>&1 \
#       && echo "{\"ts\":\"$(date +%Y-%m-%dT%H:%M:%S%z)\",\"routine\":\"commit-tests\",\"outcome\":\"ok\"}" >> "$LOG" \
#     ) &
#
# All routines are dispatched in subshells with `&` so the commit returns
# immediately. Outcomes are appended to log.jsonl as the routines finish.

exit 0
