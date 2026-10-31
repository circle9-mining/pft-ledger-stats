#!/usr/bin/env bash
# Runs resume_drill_key.sh on the NEXT key-relevant tx while the node still holds it.
#
#   keyposture/drill_next_key.sh WORKDIR [MAX_HOURS]
#
# The node keeps only ~512-1024 ledgers (online_delete), and key-relevant txs are rare (~15 per
# 24 h on testnet), so a drill range picked from the 24 h collection is usually gone already.
# This script follows validated ledgers with collect_key.py into WORKDIR-watch until the first
# key-relevant tx L appears, waits until L+AFTER is collected, stops the watcher and runs
# resume_drill_key.sh L-BEFORE L+AFTER WORKDIR. Exit 3 if nothing appears within MAX_HOURS.
# Read-only RPC on your own node (same methods as collect_key.py).
set -euo pipefail

work="${1:?usage: drill_next_key.sh WORKDIR [MAX_HOURS]}"
hours="${2:-6}"
before="${BEFORE:-100}"
after="${AFTER:-10}"
here="$(cd "$(dirname "$0")" && pwd)"
watch="$work-watch"

[ -e "$work" ] && { echo "refusing: $work exists"; exit 2; }
[ -e "$watch" ] && { echo "refusing: $watch exists"; exit 2; }

python3 -u "$here/collect_key.py" --out "$watch" --log "$watch.log" --hours "$hours" >/dev/null &
pid=$!
trap 'kill -TERM "$pid" 2>/dev/null || true' EXIT
echo "$(date -u +%FT%TZ) watching from the current validated ledger for up to ${hours} h (pid $pid)"

last_row() {  # last collected ledger, 0 before the first row
  local r
  r="$(tail -n 1 "$watch/ledgers.csv" 2>/dev/null | cut -d, -f1)"
  [[ "$r" =~ ^[0-9]+$ ]] && echo "$r" || echo 0
}
while kill -0 "$pid" 2>/dev/null; do
  # key-tx.jsonl lines are written before their ledgers.csv row, so a row with key_tx_count > 0
  # means the first line is complete
  if awk -F, 'NR > 1 && $6 > 0 { f = 1 } END { exit !f }' "$watch/ledgers.csv" 2>/dev/null; then
    read -r L ttype thash < <(python3 -c 'import json,sys
d = json.loads(open(sys.argv[1]).readline())
print(d["ledger_index"], d["tx"]["TransactionType"], d["tx"]["hash"])' "$watch/key-tx.jsonl")
    echo "$(date -u +%FT%TZ) first key-relevant tx: ledger $L $ttype $thash"
    while kill -0 "$pid" 2>/dev/null && [ "$(last_row)" -lt $((L + after)) ]; do sleep 5; done
    kill -TERM "$pid"
    wait "$pid" || true
    trap - EXIT
    echo "$(date -u +%FT%TZ) watcher stopped at ledger $(last_row); drill range $((L - before))..$((L + after))"
    exec "$here/resume_drill_key.sh" "$((L - before))" "$((L + after))" "$work"
  fi
  sleep 15
done
echo "$(date -u +%FT%TZ) no key-relevant tx within ${hours} h"
exit 3
