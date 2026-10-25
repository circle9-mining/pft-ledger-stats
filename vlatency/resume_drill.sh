#!/usr/bin/env bash
# Resume drill for collect_vlatency.py: a short real window on the local node (separate --out, the 7h window run is
# not touched), interrupted twice (SIGTERM, then SIGKILL of the python process) and restarted with the same
# command each time. vl_resume_check.py then checks:
#   - 3 session events, resumed false/true/true, same window_end; SIGTERM run wrote a stop event,
#   - the file at each interrupt (minus a torn tail after SIGKILL) is a byte-identical prefix of the final file,
#   - every line parses, no duplicate ledger or (key, ledger index, ledger hash) validation entries,
#   - validated ledgers are contiguous apart from the ranges recorded as gap events (reason restart).
# Read-only against the node (subscribe only); the 7h run in data/vlatency is a separate process and --out. Usage: bash resume_drill.sh <outdir>
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
echo "collect_vlatency.py sha256 $(sha256sum "$HERE/collect_vlatency.py" | cut -c1-64)"
HOURS=$(python3 -c "print($MINUTES/60)")
run() {
  sudo RUN_UID="$(id -u)" RUN_GID="$(id -g)" bash "$HERE/run-collector.sh" --out "$OUT" --hours "$HOURS" &
  SPID=$!; PID=""
  for _ in $(seq 50); do PID=$(pgrep -u "$(id -u)" -f "python3 .*collect_vlatency.py --out $OUT" || true); [ -n "$PID" ] && break; sleep 0.2; done
  echo "collector python pid $PID"
}
lines() { wc -l <"$OUT/vl.jsonl"; }

echo "== start 1 (window ${MINUTES} min)"; run; sleep 120
echo "== SIGTERM pid $PID after 120s"; kill -TERM "$PID"; wait "$SPID" || echo "exit status $?"
cp "$OUT/vl.jsonl" "$SNAP/term.jsonl"; echo "lines at SIGTERM: $(lines)"
sleep 30
echo "== start 2 (same command, after 30s down)"; run; sleep 90
echo "== SIGKILL pid $PID after 90s"; kill -KILL "$PID"; wait "$SPID" || echo "exit status $?"
cp "$OUT/vl.jsonl" "$SNAP/kill.jsonl"; echo "lines at SIGKILL: $(lines), last byte newline: $(tail -c1 "$OUT/vl.jsonl" | od -An -c | tr -d ' ')"
sleep 30
echo "== start 3 (same command, after 30s down), runs to window end"; run; wait "$SPID"
echo "lines at END: $(lines)"
echo "== collect output above; checks"
python3 "$HERE/vl_resume_check.py" "$OUT/vl.jsonl" "$SNAP/term.jsonl" "$SNAP/kill.jsonl"
