#!/usr/bin/env bash
# rewardflow live run against the public RPC (read-only). Run from the repo root:
#   bash rewardflow/run-live.sh ACCOUNT LOGDIR
# 1. collect with --limit 20 (forces marker pagination), stopped after 2 pages, then resumed with the
#    same command; 2. an independent one-pass collect (--limit 400) into a second file; 3. compare tx
#    lines of both files (must be identical, no duplicates); 4. analyze -> rewardflow/report/;
#    5. crosscheck (20 tx refetches + balance reconciliation).
set -uo pipefail
ACCOUNT=${1:?account}
LOGDIR=${2:?logdir}
PY=${PY:-.venv/bin/python}
D=data/rewardflow
mkdir -p "$D" "$LOGDIR"
rm -f "$D/data.jsonl" "$D/onepass.jsonl"
os() { sed -n 's/^PRETTY_NAME="\(.*\)"/\1/p' /etc/os-release; }
hdr() { echo "== OS: $(os) | $(date -u +%FT%TZ) | $1"; sha256sum rewardflow/*.py; }

{
  hdr "collect (paged, interrupted + resumed)"
  set -x
  $PY rewardflow/collect_rewardflow.py "$ACCOUNT" --out "$D/data.jsonl" --limit 20 --max-pages 2
  $PY rewardflow/collect_rewardflow.py "$ACCOUNT" --out "$D/data.jsonl" --limit 20
  $PY rewardflow/collect_rewardflow.py "$ACCOUNT" --out "$D/onepass.jsonl" --limit 400
  set +x
  echo "== compare paged+resumed vs one-pass"
  $PY - "$D/data.jsonl" "$D/onepass.jsonl" <<'PYEOF'
import json, sys
def tx(p):
    return [json.loads(x) for x in open(p) if '"kind": "tx"' in x]
a, b = tx(sys.argv[1]), tx(sys.argv[2])
ha, hb = [t["hash"] for t in a], [t["hash"] for t in b]
rng = [json.loads(x) for x in open(sys.argv[1]) if '"kind": "range"' in x][0]
print(f"paged+resumed tx={len(a)} unique={len(set(ha))} | one-pass tx={len(b)} unique={len(set(hb))}")
print(f"pinned range {rng['ledger_min']}-{rng['ledger_max']} (server complete_ledgers {rng['complete_ledgers']})")
common = [t for t in b if t["ledger_index"] <= rng["ledger_max"]]
same = sorted(ha) == sorted(t["hash"] for t in common) and len(ha) == len(set(ha))
diff = sum(1 for x, y in zip(sorted(a, key=lambda t: t["hash"]), sorted(common, key=lambda t: t["hash"])) if x != y)
print(f"one-pass tx inside pinned range: {len(common)}; field-level differences: {diff}")
print("COMPARE", "PASS" if same and diff == 0 else "FAIL")
PYEOF
} 2>&1 | tee "$LOGDIR/collect.log"

{
  hdr "analyze"
  $PY rewardflow/analyze_rewardflow.py "$D/data.jsonl" --out-dir rewardflow/report
} 2>&1 | tee "$LOGDIR/analyze.log"

{
  hdr "crosscheck"
  $PY rewardflow/crosscheck_rewardflow.py "$D/data.jsonl" --count 20
} 2>&1 | tee "$LOGDIR/crosscheck.log"
