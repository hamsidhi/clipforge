#!/usr/bin/env bash
# Benchmark the select stage of one commit on the benchmark episodes, in a throw-away git worktree (so editing the
# main checkout while it runs can't change the code under test), then run the quality check against the baseline.
#   bash scripts/bench_commit.sh <commit> <label> [compare_selection.py flags…]
# label "baseline" writes tests/fixtures/select_baseline/<id>.json instead of comparing.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
COMMIT="$1"; LABEL="$2"; shift 2
EPISODES="${BENCH_EPISODES:-TjPPIf-M6KE 67AUqNeRf6U}"
WT="$REPO/../_cf_bench/$LABEL"
PY="$REPO/.venv/Scripts/python"
export PYTHONIOENCODING=utf-8 PYTHONUTF8=1 COLUMNS=200
git -C "$REPO" worktree remove --force "$WT" 2>/dev/null || true
git -C "$REPO" worktree add --detach "$WT" "$COMMIT" >/dev/null
for v in $EPISODES; do
  if [ "$LABEL" = baseline ]; then out="$REPO/tests/fixtures/select_baseline/$v.json"; else out="$REPO/workspace/_bench/$LABEL/$v.json"; fi
  "$PY" "$WT/scripts/bench_select.py" "$v" --label "$LABEL" --workspace "$REPO/workspace" --out "$out"
done
if [ "$LABEL" != baseline ]; then
  "$PY" "$REPO/scripts/compare_selection.py" --new "$REPO/workspace/_bench/$LABEL" --workspace "$REPO/workspace" \
    --md "$REPO/workspace/_bench/$LABEL/report.md" "$@" || true
fi
git -C "$REPO" worktree remove --force "$WT"
