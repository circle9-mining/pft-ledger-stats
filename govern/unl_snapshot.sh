#!/usr/bin/env bash
# Read-only UNL snapshot for the govern analyzer: runs the admin `validators` command inside the node
# container (the admin port only answers on the container's own loopback) and keeps the fields the
# analyzer needs: quorum, trusted master keys, master -> signing key map, publisher list seq/expiration.
# Usage: sudo bash unl_snapshot.sh OUT.json   (take one at window start and one at window end)
set -euo pipefail
OUT=${1:?usage: unl_snapshot.sh OUT.json}
CONTAINER="${CONTAINER:-postfiatd}"
docker exec "$CONTAINER" timeout 10 curl -s -H 'Content-Type: application/json' \
  -d '{"method":"validators"}' http://127.0.0.1:5005 |
  jq -c --arg t "$(date -u +%FT%TZ)" '.result | select(.status == "success") | {t: $t,
    validation_quorum, trusted_validator_keys, signing_keys,
    publisher_lists: [.publisher_lists[]? | {seq, expiration, available, n: (.list | length)}]}' >"$OUT.tmp"
[ -s "$OUT.tmp" ] || { rm -f "$OUT.tmp"; echo "validators RPC failed" >&2; exit 1; }
mv "$OUT.tmp" "$OUT"
jq -r '"UNL snapshot \(.t): \(.trusted_validator_keys | length) trusted keys, quorum \(.validation_quorum)"' "$OUT"
