#!/usr/bin/env bash
# Resume drill for collect_tx.py: a short real window on the local node, interrupted twice
# (SIGTERM, then SIGKILL) and restarted with the same command each time. Checks afterwards that
#   - there is 1 WINDOW START line and every restart logged RESUME with that same window,
#   - rows written before each interrupt are a byte-identical prefix of the final CSVs
#     (after the collector's own repair of a torn last line / orphan tx rows, which it logs),
#   - there are no duplicate tx hashes or ledgers, and every CSV line has the right column count,
#   - ledgers.csv is contiguous apart from the ledgers recorded in gaps.csv,
#   - every ledger's txn_count equals its number of tx.csv rows,
#   - no curl is left running inside the container.
# Read-only against the node (server_info + ledger only). Usage: resume_drill.sh <outdir>
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
run() { python3 -I "$HERE/collect_tx.py" --out "$OUT" --log "$OUT/collect.log" --hours "$HOURS" \
          --sample-interval 60 >/dev/null & PID=$!; }
counts() {
  for f in tx ledgers gaps; do
    if [ -f "$OUT/$f.csv" ]; then printf '%s=%s ' "$f" "$(($(wc -l <"$OUT/$f.csv") - 1))"
    else printf '%s=0 ' "$f"; fi
  done; echo
}
snap() { for f in tx ledgers gaps; do [ -f "$OUT/$f.csv" ] && cp "$OUT/$f.csv" "$SNAP/$1-$f.csv"; done; true; }

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
echo "== gaps.csv"; cat "$OUT/gaps.csv" 2>/dev/null || echo "(none)"
echo "== checks"
python3 -I "$HERE/resume_check.py" "$OUT" "$SNAP"
echo "== curl processes left in container: $(sudo docker exec postfiatd pgrep -c curl || true)"
