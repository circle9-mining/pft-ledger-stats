#!/usr/bin/env bash
# Rebuild REPORT.md from the published .gz files only and compare with the committed report.
set -euo pipefail
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
cp data/txmix/window.json "$T/state.json"
for f in data/txmix/*.gz; do gunzip -c "$f" > "$T/$(basename "$f" .gz)"; echo "unpacked $f ($(wc -l < "$T/$(basename "$f" .gz)") lines)"; done
.venv/bin/python txmix/analyze_tx.py --data "$T" --out "$T/report" --no-charts
if diff <(grep -v '^!\[' txmix/report/REPORT.md) <(grep -v '^!\[' "$T/report/REPORT.md"); then
  echo "REPORT.md identical (chart image lines excluded: --no-charts)"
else
  echo "REPORT.md DIFFERS"; exit 1
fi
