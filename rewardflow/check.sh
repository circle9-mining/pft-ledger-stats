#!/usr/bin/env bash
# rewardflow offline checks: ruff, shellcheck, unit tests (fixtures only, no network), and two
# mutants that the tests must catch. Run from the repo root: bash rewardflow/check.sh
set -uo pipefail
PY=${PY:-.venv/bin/python}
RUFF=${RUFF:-.venv/bin/ruff}
fail=0
echo "== OS: $(sed -n 's/^PRETTY_NAME="\(.*\)"/\1/p' /etc/os-release) | python: $($PY -V 2>&1) | $(date -u +%FT%TZ)"
echo "== sha256"
sha256sum rewardflow/*.py rewardflow/*.sh tests/test_rewardflow.py
echo "== ruff"
$RUFF check rewardflow tests/test_rewardflow.py || fail=1
echo "== shellcheck"
if shellcheck rewardflow/*.sh; then echo "shellcheck clean"; else fail=1; fi
echo "== unit tests"
$PY -m pytest -q -rA -p no:cacheprovider tests/test_rewardflow.py 2>&1 | grep -E '^(PASSED|FAILED|ERROR)|passed|failed' || true
$PY -m pytest -q -p no:cacheprovider tests/test_rewardflow.py >/dev/null 2>&1 || { echo "unit tests FAIL"; fail=1; }

mutant() {  # name file sed-expression: the suite must FAIL against the mutant
  local name=$1 file=$2 expr=$3 tmp
  tmp=$(mktemp -d)
  mkdir -p "$tmp/rewardflow" "$tmp/tests"
  cp rewardflow/*.py "$tmp/rewardflow/"
  cp tests/test_rewardflow.py "$tmp/tests/"
  sed -i "$expr" "$tmp/rewardflow/$file"
  if cmp -s "rewardflow/$file" "$tmp/rewardflow/$file"; then
    echo "MUTANT $name NOT APPLIED (sed matched nothing)"; fail=1
  elif (cd "$tmp" && "$OLDPWD/$PY" -m pytest -q -p no:cacheprovider tests/test_rewardflow.py >"$tmp/out" 2>&1); then
    echo "MUTANT $name SURVIVED"; fail=1
  else
    echo "MUTANT $name caught: $(grep -cE '^FAILED' "$tmp/out" || true) failing tests:" \
      "$(grep -oE '^FAILED [^ ]+' "$tmp/out" | sed 's/.*:://' | tr '\n' ' ')"
  fi
  rm -rf "$tmp"
}
echo "== mutants"
mutant M1-resume-after-last-ledger collect_rewardflow.py 's/start = last if last is not None/start = last + 1 if last is not None/'
mutant M2-amount-not-delivered analyze_rewardflow.py 's/d = native(t\["delivered_amount"\])/d = native(t["amount"])/'
mutant M3-count-failed-as-paid analyze_rewardflow.py 's/if t\["result"\] != "tesSUCCESS":/if False:/'
mutant M4-no-transient-retry crosscheck_rewardflow.py 's/tries=4, wait=2.0/tries=1, wait=2.0/'
echo "== RESULT $([ $fail = 0 ] && echo PASS || echo FAIL)"
exit $fail
