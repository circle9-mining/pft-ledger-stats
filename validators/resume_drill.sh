#!/usr/bin/env bash
# Resume drill for collect_validations.py: a short real window on the local node, interrupted twice
# (SIGTERM, then SIGKILL) and restarted with the same command each time. Checks afterwards that
#   - the restart logged RESUME with the same window as the first WINDOW line,
#   - rows written before each interrupt are a byte-identical prefix of the final CSVs (nothing lost/rewritten),
#   - there are no duplicate validations (ledger_index, ledger_hash, signing_key) or ledgers,
#   - every CSV line parses with the right column count (no torn row after SIGKILL),
#   - ledgers.csv is contiguous apart from the ranges recorded in gaps.csv,
#   - no relay process is left running inside the container.
# Read-only against the node (same allowlisted commands as the 24h run). Usage: resume_drill.sh <outdir>
set -euo pipefail
OUT=${1:?usage: resume_drill.sh <outdir>}
HERE=$(cd "$(dirname "$0")" && pwd)
MINUTES=${MINUTES:-8}
[ -e "$OUT" ] && { echo "refusing to reuse existing $OUT"; exit 2; }
mkdir -p "$OUT"
SNAP=$(mktemp -d)
trap 'rm -rf "$SNAP"' EXIT

# shellcheck disable=SC1091
echo "OS: $(. /etc/os-release && echo "$PRETTY_NAME") | python: $(python3 -V 2>&1) | $(date -u +%FT%TZ)"
HOURS=$(python3 -c "print($MINUTES/60)")
run() { python3 -I "$HERE/collect_validations.py" --out "$OUT" --hours "$HOURS" & PID=$!; }
counts() {
  for f in validations ledgers manifests gaps; do
    printf '%s=%s ' "$f" "$(($(wc -l <"$OUT/$f.csv") - 1))"
  done; echo
}
snap() { for f in validations ledgers manifests gaps; do cp "$OUT/$f.csv" "$SNAP/$1-$f.csv"; done; }

echo "== start 1 (window ${MINUTES} min)"; run; sleep 120
echo "== SIGTERM pid $PID after 120s"; kill -TERM "$PID"; wait "$PID" || echo "exit status $?"
snap term; echo "rows at SIGTERM: $(counts)"
sleep 30
echo "== start 2 (same command, after 30s down)"; run; sleep 90
echo "== SIGKILL pid $PID after 90s"; kill -KILL "$PID"; wait "$PID" || echo "exit status $?"
snap kill; echo "rows at SIGKILL: $(counts)"
sleep 30
echo "== start 3 (same command, after 30s down), runs to window end"; run; wait "$PID"
echo "rows at END: $(counts)"

echo "== collect.log"
cat "$OUT/collect.log"
echo "== gaps.csv"; cat "$OUT/gaps.csv"
echo "== checks"
python3 -I "$HERE/resume_check.py" "$OUT" "$SNAP"
echo "== relay processes left in container: $(sudo docker exec postfiatd pgrep -fc 'relay 127.0.0.1' || true)"
