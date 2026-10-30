#!/usr/bin/env bash
# Resume drill for keyposture/collect_key.py on a fixed, already-validated ledger range.
#
#   keyposture/resume_drill_key.sh FIRST LAST [WORKDIR]
#
# 1. reference: one uninterrupted run over FIRST..LAST
# 2. drill: same range, SIGTERM after ~TERM_AFTER s, resume, SIGKILL after ~KILL_AFTER s,
#    resume, run to the end
# 3. compare: key-tx.jsonl byte-identical and NON-EMPTY (pick a range with key txs),
#    ledgers.csv identical without fetched_utc,
#    every ledger FIRST..LAST exactly once, gaps.csv absent/empty in both
# Exit 0 only if every check passes. Read-only RPC (server_info, ledger) on your own node.
# shellcheck disable=SC2016  # check() bodies are eval'd on purpose, expanded at check time
set -euo pipefail

first="${1:?usage: resume_drill_key.sh FIRST LAST [WORKDIR]}"
last="${2:?usage: resume_drill_key.sh FIRST LAST [WORKDIR]}"
work="${3:-data/keyposture/drill}"
term_after="${TERM_AFTER:-25}"
kill_after="${KILL_AFTER:-25}"
here="$(cd "$(dirname "$0")" && pwd)"
col=(python3 -u "$here/collect_key.py" --start "$first" --stop-at "$last")

[ -e "$work" ] && { echo "refusing: $work exists"; exit 2; }
mkdir -p "$work"
ref="$work/ref" drl="$work/drill"

echo "== reference run $first..$last"
"${col[@]}" --out "$ref" --log "$work/ref.log" >/dev/null

echo "== drill run: SIGTERM after ${term_after}s"
"${col[@]}" --out "$drl" --log "$work/drill.log" >/dev/null &
pid=$!
sleep "$term_after"
kill -TERM "$pid"
wait "$pid" || true
echo "after SIGTERM: ledgers $(($(wc -l <"$drl/ledgers.csv") - 1)), last $(tail -1 "$drl/ledgers.csv" | cut -d, -f1)"

echo "== resume 1, SIGKILL after ${kill_after}s"
"${col[@]}" --out "$drl" --log "$work/drill.log" >/dev/null &
pid=$!
sleep "$kill_after"
kill -KILL "$pid"
wait "$pid" || true
echo "after SIGKILL: ledgers $(($(wc -l <"$drl/ledgers.csv") - 1)), last $(tail -1 "$drl/ledgers.csv" | cut -d, -f1)"

echo "== resume 2 to the end"
"${col[@]}" --out "$drl" --log "$work/drill.log" >/dev/null
grep -E "RESUME|STOP|SUMMARY" "$work/drill.log"

fail=0
check() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fail=1; fi; }
strip() { cut -d, -f1-6 "$1"; }
check "reference key-tx.jsonl non-empty ($(wc -l <"$ref/key-tx.jsonl") lines)" '[ -s "$ref/key-tx.jsonl" ]'
check "key-tx.jsonl identical ($(wc -l <"$ref/key-tx.jsonl") lines)" \
  'cmp -s "$ref/key-tx.jsonl" "$drl/key-tx.jsonl"'
check "ledgers.csv identical without fetched_utc" 'diff -q <(strip "$ref/ledgers.csv") <(strip "$drl/ledgers.csv") >/dev/null'
check "every ledger $first..$last exactly once" \
  'diff -q <(seq "$first" "$last") <(tail -n +2 "$drl/ledgers.csv" | cut -d, -f1) >/dev/null'
check "no gaps" '[ ! -s "$ref/gaps.csv" ] && [ ! -s "$drl/gaps.csv" ]'
check "key_tx_count sum == jsonl lines" \
  '[ "$(tail -n +2 "$drl/ledgers.csv" | cut -d, -f6 | paste -sd+ | bc)" -eq "$(wc -l <"$drl/key-tx.jsonl")" ]'
echo "sha256 ref   $(sha256sum <"$ref/key-tx.jsonl" | cut -d' ' -f1)"
echo "sha256 drill $(sha256sum <"$drl/key-tx.jsonl" | cut -d' ' -f1)"
[ "$fail" -eq 0 ] && echo "RESULT: PASS" || echo "RESULT: FAIL"
exit "$fail"
