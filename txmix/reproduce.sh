#!/usr/bin/env bash
# Rebuild REPORT.md from the published .gz files only and compare it byte-for-byte with the committed report.
set -euo pipefail
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
cp data/txmix/window.json "$T/state.json"
for f in data/txmix/*.gz; do gunzip -c "$f" > "$T/$(basename "$f" .gz)"; echo "unpacked $f ($(wc -l < "$T/$(basename "$f" .gz)") lines)"; done
.venv/bin/python txmix/analyze_tx.py --data "$T" --out "$T/report" --no-charts
committed=$(sha256sum < txmix/report/REPORT.md | cut -d' ' -f1)
rebuilt=$(sha256sum < "$T/report/REPORT.md" | cut -d' ' -f1)
echo "sha256 committed txmix/report/REPORT.md: $committed"
echo "sha256 rebuilt   <tmp>/report/REPORT.md: $rebuilt"
if cmp txmix/report/REPORT.md "$T/report/REPORT.md"; then
  echo "REPORT.md byte-identical (cmp exit 0; whole file incl. chart link lines; PNGs not rebuilt: --no-charts)"
else
  echo "REPORT.md DIFFERS"; exit 1
fi
