#!/usr/bin/env bash
# Writes keyposture/REPORT.md from the published data: every number comes from reconcile.json,
# stats.json/stats.md, the snapshot state files and the crosscheck SUMMARY lines (no hand-typed figures).
#
#   keyposture/report_key.sh DATA_DIR CROSSCHECK_LOG NEGATIVE_LOG > keyposture/REPORT.md
set -euo pipefail

d="${1:?usage: report_key.sh DATA_DIR CROSSCHECK_LOG NEGATIVE_LOG}"
xc="${2:?usage: report_key.sh DATA_DIR CROSSCHECK_LOG NEGATIVE_LOG}"
xn="${3:?usage: report_key.sh DATA_DIR CROSSCHECK_LOG NEGATIVE_LOG}"
r="$d/reconcile.json"
s="$d/stats.json"

[ "$(jq -r .result "$r")" = PASS ] || { echo "refusing: reconcile.json is not a PASS" >&2; exit 2; }
[ "$(jq -r '.negative // "null"' "$r")" = null ] || { echo "refusing: --negative reconciliation" >&2; exit 2; }
sum_ok="$(grep -m1 '^SUMMARY .*negative=False' "$xc")" || { echo "no SUMMARY in $xc" >&2; exit 2; }
sum_neg="$(grep -m1 '^SUMMARY .*negative=True' "$xn")" || { echo "no SUMMARY in $xn" >&2; exit 2; }

ja() { jq -r "$1" "$d/snapA-state.json"; }
jb() { jq -r "$1" "$d/snapB-state.json"; }
jr() { jq -r "$1" "$r"; }

cat <<EOF
# keyposture: signing-key posture of every testnet account, ledger $(ja .ledger_index) -> $(jb .ledger_index)

- Snapshot A: ledger $(ja .ledger_index) (closed $(ja .close_time_iso)), hash \`$(ja .ledger_hash)\`
- Snapshot B: ledger $(jb .ledger_index) (closed $(jb .close_time_iso)), hash \`$(jb .ledger_hash)\`
- Window: $(jr .ledgers) ledgers (A+1..B), $(jr .key_tx) key-relevant transactions ($(jr '.tx_types | to_entries | map("\(.key) \(.value)") | join(", ")')), $(jr .key_tx_with_effect) with a posture effect
- Accounts: $(jr .snap_a.accounts) at A, $(jr .snap_b.accounts) at B ($(jr .created) created, $(jr .deleted) deleted); SignerList objects: $(jr .snap_a.signer_lists) at A, $(jr .snap_b.signer_lists) at B

## Reconciliation

Result: **$(jr .result)**, residuals: $(jr '.residuals | length').

| class | A | in | out | A + in - out | B |
|---|---:|---:|---:|---:|---:|
$(jr '.per_class | to_entries[] | "| \(.key) | \(.value.A) | \(.value.in) | \(.value.out) | \(.value.expected_B) | \(.value.B) |"')

## Public-RPC crosscheck

- \`$sum_ok\`
- \`--negative\`: \`$sum_neg\`

## Classes, weak configurations, reward accounts and top balances

![accounts by balance bucket and class at B](balances.png)

$(cat "$d/stats.md")

Weak-flag counts at A: $(jq -r '.weak_a | if length == 0 then "none" else (to_entries | map("\(.key) \(.value)") | join(", ")) end' "$s"); at B: $(jq -r '.weak_b | if length == 0 then "none" else (to_entries | map("\(.key) \(.value)") | join(", ")) end' "$s").

Addresses are truncated here; the raw snapshots and \`data/tx/key-tx.jsonl\` keep full addresses (public ledger data).
EOF
