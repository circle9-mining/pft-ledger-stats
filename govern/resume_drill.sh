#!/usr/bin/env bash
# Resume drill for collect_govern.py: a real window on the local node (separate --out, the 24h run is not
# touched), interrupted twice (SIGTERM, then SIGKILL of the python process) and restarted with the same
# command each time. Votes only exist on one ledger in 256 (~17 min), so the window is 25 min by default.
# A SIGKILL usually lands between two complete writes. If the file still ends with a newline after the
# kill, the drill appends a partial line (labelled INJECTED below) to stand for a write the kill cut off,
# so the restart has to repair a torn tail every time. gov_resume_check.py then checks:
#   - 3 session events, resumed false/true/true, same window_end; SIGTERM run wrote a stop event,
#   - the file at each interrupt (minus the torn tail) is a byte-identical prefix of the final file,
#     and session 3 recorded exactly the torn bytes it cut,
#   - every line parses, no duplicate ledger / vote / snapshot entries, votes only on voting ledgers,
#   - validated ledgers are contiguous apart from the ranges recorded as gap events (reason restart),
#   - the window covered a voting ledger and its flag ledger, with both ledger_entry snapshots.
# Read-only against the node (subscribe + ledger_entry only). Usage: bash resume_drill.sh <outdir>
set -euo pipefail
OUT=${1:?usage: resume_drill.sh <outdir>}
HERE=$(cd "$(dirname "$0")" && pwd)
MINUTES=${MINUTES:-25}
[ -e "$OUT" ] && { echo "refusing to reuse existing $OUT"; exit 2; }
mkdir -p "$OUT"
SNAP=$(mktemp -d)
trap 'rm -rf "$SNAP"' EXIT
# shellcheck disable=SC1091
echo "OS: $(. /etc/os-release && echo "$PRETTY_NAME") | python: $(python3 -V 2>&1) | $(date -u +%FT%TZ)"
echo "collect_govern.py sha256 $(sha256sum "$HERE/collect_govern.py" | cut -c1-64)"
HOURS=$(python3 -c "print($MINUTES/60)")
F="$OUT/govern.jsonl"
run() {
  sudo RUN_UID="$(id -u)" RUN_GID="$(id -g)" bash "$HERE/run-collector.sh" --out "$OUT" --hours "$HOURS" &
  SPID=$!; PID=""
  for _ in $(seq 50); do PID=$(pgrep -u "$(id -u)" -f "python3 .*collect_govern.py --out $OUT" || true); [ -n "$PID" ] && break; sleep 0.2; done
  echo "collector python pid $PID"
}
lines() { wc -l <"$F"; }
lastbyte() { tail -c1 "$F" | od -An -c | tr -d ' '; }

echo "== start 1 (window ${MINUTES} min)"; run; sleep 120
echo "== SIGTERM pid $PID after 120s"; kill -TERM "$PID"; wait "$SPID" || echo "exit status $?"
cp "$F" "$SNAP/term.jsonl"; echo "lines at SIGTERM: $(lines)"
sleep 30
echo "== start 2 (same command, after 30s down)"; run; sleep 90
echo "== SIGKILL pid $PID after 90s"; kill -KILL "$PID"; wait "$SPID" || echo "exit status $?"
echo "lines at SIGKILL: $(lines), last byte: $(lastbyte)"
if [ "$(tail -c1 "$F" | od -An -tx1 | tr -d ' ')" = "0a" ]; then
  TORN='{"ev":"ledger","li":'
  printf '%s' "$TORN" >>"$F"
  echo "INJECTED torn tail: the kill left complete lines, appended ${#TORN} bytes: $TORN"
else
  echo "natural torn tail left by SIGKILL: $(tail -c 200 "$F" | tr '\n' '|' | sed 's/.*|//' | wc -c) bytes"
fi
cp "$F" "$SNAP/kill.jsonl"
sleep 30
echo "== start 3 (same command, after 30s down), runs to window end"; run; wait "$SPID"
echo "lines at END: $(lines)"
echo "== session events"; grep '"ev":"session"' "$F"
echo "== collect output above; checks"
python3 "$HERE/gov_resume_check.py" "$F" "$SNAP/term.jsonl" "$SNAP/kill.jsonl"
