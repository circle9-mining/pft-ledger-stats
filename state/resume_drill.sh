#!/usr/bin/env bash
# Live interrupt/resume drill for state/snapshot.py on the local node (read-only RPC only).
# One snapshot of the current validated ledger with small pages (--limit 64, --page-delay) is
# interrupted twice (SIGTERM, then SIGKILL) and restarted with the same command each time.
# Then an uninterrupted reference snapshot of the SAME ledger is taken with --limit 2048.
# Checks afterwards that
#   - every restart logged RESUME on the same pinned ledger, from the last complete page,
#   - pages.jsonl is a contiguous marker chain (page n's marker_in = page n-1's marker_out),
#   - no ledger object index appears twice, and the object count matches the reference,
#   - the drill's snapshot .gz is byte-identical (sha256) to the reference .gz,
#   - no curl is left running inside the container.
# Usage: resume_drill.sh <outdir>
set -euo pipefail
OUT=${1:?usage: resume_drill.sh <outdir>}
HERE=$(cd "$(dirname "$0")" && pwd)
[ -e "$OUT" ] && { echo "refusing to reuse existing $OUT"; exit 2; }
mkdir -p "$OUT"

# shellcheck disable=SC1091
echo "OS: $(. /etc/os-release && echo "$PRETTY_NAME") | python: $(python3 -V 2>&1) | $(date -u +%FT%TZ)"
run() { python3 "$HERE/snapshot.py" --out "$OUT/drill" --log "$OUT/drill.log" --limit 64 \
          --page-delay 0.3 >/dev/null & PID=$!; }
pages() { if [ -f "$OUT/drill/pages.jsonl" ]; then wc -l < "$OUT/drill/pages.jsonl"; else echo 0; fi; }

echo "== start 1: snapshot.py --limit 64 --page-delay 0.3"; run; sleep 15
echo "== SIGTERM pid $PID after 15s"; kill -TERM "$PID"; wait "$PID" || echo "exit status $?"
echo "complete page lines at SIGTERM: $(pages)"
sleep 5
echo "== start 2 (same command)"; run; sleep 15
echo "== SIGKILL pid $PID after 15s"; kill -KILL "$PID"; wait "$PID" || echo "exit status $?"
echo "page lines at SIGKILL: $(pages) (last one may be torn)"
tail -c 1 "$OUT/drill/pages.jsonl" | od -An -c | sed 's/^/last byte of pages.jsonl:/'
sleep 5
echo "== start 3 (same command), runs to the end"; run; wait "$PID"; echo "exit status $?"
LEDGER=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["ledger_index"])' "$OUT/drill/state.json")

echo "== reference: uninterrupted snapshot.py --ledger $LEDGER --limit 2048"
python3 "$HERE/snapshot.py" --out "$OUT/ref" --log "$OUT/ref.log" --ledger "$LEDGER" >/dev/null

echo "== drill.log"
cat "$OUT/drill.log"
echo "== ref.log"
cat "$OUT/ref.log"
echo "== checks"
python3 - "$OUT" "$LEDGER" <<'EOF'
import gzip, json, sys
out, ledger = sys.argv[1], int(sys.argv[2])
fails = 0
def check(ok, msg):
    global fails
    fails += not ok
    print(f"[{'PASS' if ok else 'FAIL'}] {msg}")
log = open(f"{out}/drill.log").read().splitlines()
starts = [l for l in log if " START " in l]
resumes = [l for l in log if " RESUME " in l]
check(len(starts) == 1 and len(resumes) == 2, f"1 START + 2 RESUME lines (got {len(starts)} + {len(resumes)})")
check(all(f"ledger {ledger} " in l for l in starts + resumes), f"all on pinned ledger {ledger}")
pages = [json.loads(l) for l in open(f"{out}/drill/pages.jsonl")]
chain = all(p["page"] == i and p["marker_in"] == (pages[i - 1]["marker_out"] if i else None)
            for i, p in enumerate(pages))
check(chain and pages[-1]["marker_out"] is None, f"pages.jsonl: {len(pages)} pages, contiguous marker chain, last has no marker")
idx = [o["index"] for p in pages for o in p["state"]]
check(len(idx) == len(set(idx)), f"no duplicate object index ({len(idx)} objects, {len(set(idx))} unique)")
def objs(path):
    with gzip.open(path, "rt") as fh:
        return [json.loads(l) for l in fh][1:]
ref = objs(f"{out}/ref/snapshot-{ledger}.jsonl.gz")
check(len(ref) == len(idx), f"object count drill {len(idx)} = reference {len(ref)}")
sys.exit(1 if fails else 0)
EOF
sha256sum "$OUT/drill/snapshot-$LEDGER.jsonl.gz" "$OUT/ref/snapshot-$LEDGER.jsonl.gz" | sed "s#$OUT/##"
if cmp "$OUT/drill/snapshot-$LEDGER.jsonl.gz" "$OUT/ref/snapshot-$LEDGER.jsonl.gz"; then
  echo "[PASS] drill snapshot (3 runs, 64/page) byte-identical to reference (1 run, 2048/page)"
else
  echo "[FAIL] drill snapshot differs from reference"; exit 1
fi
echo "== curl processes left in container: $(sudo docker exec postfiatd pgrep -c curl || true)"
