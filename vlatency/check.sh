#!/usr/bin/env bash
# vlatency offline checks: ruff, shellcheck, unit tests (fixtures and fakes only, no network), and
# mutants that the tests must catch. Run from the repo root: bash vlatency/check.sh
set -uo pipefail
PY=${PY:-.venv/bin/python}
RUFF=${RUFF:-.venv/bin/ruff}
TESTS="tests/test_vlatency_collect.py tests/test_vlatency_analyze.py tests/test_vlatency_resume.py"
fail=0
echo "== OS: $(sed -n 's/^PRETTY_NAME="\(.*\)"/\1/p' /etc/os-release) | python: $($PY -V 2>&1) | $(date -u +%FT%TZ)"
echo "== sha256"
# shellcheck disable=SC2086
sha256sum vlatency/*.py vlatency/*.sh $TESTS
echo "== ruff"
# shellcheck disable=SC2086
$RUFF check vlatency $TESTS || fail=1
echo "== shellcheck"
if shellcheck vlatency/*.sh; then echo "shellcheck clean"; else fail=1; fi
echo "== unit tests"
# shellcheck disable=SC2086
$PY -m pytest -q -rA -p no:cacheprovider $TESTS 2>&1 | grep -E '^(PASSED|FAILED|ERROR)|passed|failed' || true
# shellcheck disable=SC2086
$PY -m pytest -q -p no:cacheprovider $TESTS >/dev/null 2>&1 || { echo "unit tests FAIL"; fail=1; }

mutant() {  # name file sed-expression: the suite must FAIL against the mutant
  local name=$1 file=$2 expr=$3 tmp
  tmp=$(mktemp -d)
  mkdir -p "$tmp/vlatency" "$tmp/tests" "$tmp/txflow" "$tmp/pftstats"
  cp vlatency/*.py "$tmp/vlatency/"
  cp txflow/wsclient.py "$tmp/txflow/"
  cp pftstats/*.py "$tmp/pftstats/"
  # shellcheck disable=SC2086
  cp $TESTS "$tmp/tests/"
  sed -i "$expr" "$tmp/vlatency/$file"
  if cmp -s "vlatency/$file" "$tmp/vlatency/$file"; then
    echo "MUTANT $name NOT APPLIED (sed matched nothing)"; fail=1
  elif (cd "$tmp" && "$OLDPWD/$PY" -m pytest -q -p no:cacheprovider tests >"$tmp/out" 2>&1); then
    echo "MUTANT $name SURVIVED"; fail=1
  else
    echo "MUTANT $name caught: $(grep -cE '^FAILED' "$tmp/out" || true) failing tests:" \
      "$(grep -oE '^FAILED [^ ]+' "$tmp/out" | sed 's/.*:://' | tr '\n' ' ')"
  fi
  rm -rf "$tmp"
}
echo "== mutants"
mutant M1-no-ripple-epoch analyze_vlatency.py 's/return rt + RIPPLE_EPOCH/return rt/'
mutant M2-floor-not-round analyze_vlatency.py 's/    t += res \/\/ 2/    pass/'
mutant M3-analyzer-no-dedupe analyze_vlatency.py 's/if key in seen:/if False:/'
mutant M4-coverage-ignores-span analyze_vlatency.py 's/if lis\[0\] <= i <= lis\[-1\]\]/]/'
mutant M5-collector-no-resume-dedupe collect_vlatency.py 's/if self.s.has(li, k, lh):/if False:/'
mutant M6-no-torn-tail-cut collect_vlatency.py 's/fh.truncate(keep)/fh.flush()/'
mutant M7-crosscheck-no-retry crosscheck_vlatency.py 's/default=4)/default=1)/'
mutant M8-checker-ignores-dup-vals vl_resume_check.py 's/vals and len(vals) == len(set(vals))/bool(vals)/'
echo "== RESULT $([ $fail = 0 ] && echo PASS || echo FAIL)"
exit $fail
