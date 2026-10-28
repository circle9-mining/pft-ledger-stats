#!/usr/bin/env bash
# Corrupted-reconstruction negative control for analyze_nunl.py (read-only, works on a temp copy).
# 1. control: the unmodified dataset must analyze with exit 0;
# 2. corrupt: inject a fake DisabledValidator into the NegativeUNL object of one random flag ledger
#    (at F and F+1, so only the list-transition check can catch it). The analyzer must exit 1 and
#    name that flag ledger in a "DisabledValidators ... != expected" check failure.
# Usage: bash nunl/negative_control.sh data/nunl/final/nunl.jsonl [seed]
set -uo pipefail
HIST=${1:?usage: negative_control.sh NUNL.jsonl [seed]}
SEED=${2:-$(date +%s)}
PY=${PY:-python3}
here="$(cd "$(dirname "$0")" && pwd)"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
fail=0

echo "== control: unmodified copy (seed $SEED)"
cp "$HIST" "$tmp/clean.jsonl"
"$PY" "$here/analyze_nunl.py" --hist "$tmp/clean.jsonl" --out "$tmp/clean"
rc=$?
echo "control exit=$rc"
[ "$rc" = 0 ] || { echo "FAIL: control must exit 0"; fail=1; }

echo "== corrupt: fake DisabledValidator at one random flag ledger"
target=$("$PY" - "$HIST" "$tmp/bad.jsonl" "$SEED" <<'EOF'
import json, random, sys
src, dst, seed = sys.argv[1], sys.argv[2], int(sys.argv[3])
evs = [json.loads(line) for line in open(src)]
flags = [e["li"] for e in evs if e.get("ev") == "flag"]
target = random.Random(seed).choice(flags[1:])  # not the first flag: it has no predecessor to check against
fake = "ED" + "AB" * 32
for e in evs:
    if e.get("ev") == "flag" and e["li"] == target:
        v = e["nunl"] or {"disabled": [], "to_disable": None, "to_reenable": None}
        v = dict(v, disabled=sorted(v["disabled"] + [fake]))
        e["nunl"] = e["next"] = v
with open(dst, "w") as fh:
    for e in evs:
        fh.write(json.dumps(e, separators=(",", ":")) + "\n")
print(target)
EOF
)
echo "corrupted flag ledger: $target"
"$PY" "$here/analyze_nunl.py" --hist "$tmp/bad.jsonl" --out "$tmp/bad" | tee "$tmp/bad.out"
rc=${PIPESTATUS[0]}
echo "corrupt exit=$rc"
[ "$rc" = 1 ] || { echo "FAIL: corrupted reconstruction must exit 1"; fail=1; }
if grep -q "CHECK FAIL F$target: DisabledValidators" "$tmp/bad.out"; then
  echo "expected failure message present (F$target DisabledValidators)"
else
  echo "FAIL: no DisabledValidators failure for F$target"
  fail=1
fi
echo "== RESULT $([ $fail = 0 ] && echo PASS || echo FAIL)"
exit $fail
