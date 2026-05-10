#!/usr/bin/env bash
# scripts/daily-digest.sh
#
# Pure-shell daily digest. No Claude tokens — used when meta.budget is
# `low` or `medium` and the consumer wants the cheapest possible digest.
#
# Produces Markdown on stdout. Tolerates missing `gh` (prints a stub
# section) and missing/empty git history (prints a stub section) so the
# routine never errors out and the dashboard always gets something.
#
# Usage:
#   scripts/daily-digest.sh                          # since 00:00 today
#   scripts/daily-digest.sh "1 day ago"              # since N units ago
#   scripts/daily-digest.sh "00:00 today" --no-gh    # skip gh entirely
#
# Catalog hook: archetypes.daily-digest can branch on meta.budget and
# dispatch this script instead of the LLM prompt. See PRD .iteration/
# goal.md "Token frugality" block.

set -euo pipefail

SINCE="${1:-00:00 today}"
SKIP_GH=""
for arg in "$@"; do
    case "$arg" in
        --no-gh) SKIP_GH=1 ;;
    esac
done

DATE=$(date +%Y-%m-%d)

# --- Header --------------------------------------------------------------
echo "# Daily digest — ${DATE}"
echo

# --- Commits -------------------------------------------------------------
echo "## Commits since ${SINCE}"
commits=$(git log --since="${SINCE}" --pretty=format:'- %h %s (%an)' 2>/dev/null || true)
if [ -n "${commits}" ]; then
    echo "${commits}"
else
    echo "(no commits in window)"
fi
echo
echo

# --- PR activity ---------------------------------------------------------
echo "## PR activity (last 24h)"
if [ -n "${SKIP_GH}" ]; then
    echo "(gh skipped — --no-gh)"
elif ! command -v gh >/dev/null 2>&1; then
    echo "(gh not installed)"
elif ! gh auth status >/dev/null 2>&1; then
    echo "(gh not authenticated)"
else
    # `updated:` filter shape varies; fall back to filtering by updatedAt
    # in JSON so we don't depend on a date-arithmetic flag (date -d isn't
    # portable across GNU/BSD).
    pr_output=$(gh pr list --state all --limit 20 \
        --json number,title,state,updatedAt 2>/dev/null || echo "[]")
    # Use jq if available; otherwise emit a one-liner stub.
    if command -v jq >/dev/null 2>&1; then
        rendered=$(echo "${pr_output}" | jq -r '
            [.[] | select(.updatedAt > (now - 86400 | strftime("%Y-%m-%dT%H:%M:%SZ")))]
            | if length == 0 then "(no PR activity in window)"
              else .[] | "- #\(.number) \(.title) (\(.state))"
              end
        ' 2>/dev/null || echo "(jq failed to parse PR JSON)")
        echo "${rendered}"
    else
        echo "(jq not installed — install for PR activity details)"
    fi
fi
echo
