#!/usr/bin/env bash
# Rebuild state/report/REPORT.md + report.json from the published snapshot .gz only and compare
# them byte-for-byte (sha256 + cmp) with the committed files. Run from the repo root.
set -euo pipefail
SNAP=${1:-data/state/snapshot-7429722.jsonl.gz}
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
echo "snapshot: $SNAP sha256 $(sha256sum < "$SNAP" | cut -d' ' -f1)"
.venv/bin/python state/analyze.py "$SNAP" --out "$T/report" --no-charts | sed "s#$T#<tmp>#"
rc=0
for f in REPORT.md report.json; do
  echo "sha256 committed state/report/$f: $(sha256sum < "state/report/$f" | cut -d' ' -f1)"
  echo "sha256 rebuilt   <tmp>/report/$f: $(sha256sum < "$T/report/$f" | cut -d' ' -f1)"
  if cmp -s "state/report/$f" "$T/report/$f"; then echo "$f byte-identical (cmp exit 0)"
  else echo "$f DIFFERS"; rc=1; fi
done
echo "(PNG charts are not rebuilt: --no-charts; REPORT.md always links them)"
exit "$rc"
