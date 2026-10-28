#!/usr/bin/env bash
# nunl offline checks: ruff, shellcheck, unit tests (synthetic fixtures and fakes only, no network), and
# mutants (threshold and logic mutations) that the tests must catch. Run from the repo root: bash nunl/check.sh
set -uo pipefail
PY=${PY:-.venv/bin/python}
RUFF=${RUFF:-.venv/bin/ruff}
TESTS="tests/test_nunl.py"
fail=0
echo "== OS: $(sed -n 's/^PRETTY_NAME="\(.*\)"/\1/p' /etc/os-release) | python: $($PY -V 2>&1) | $(date -u +%FT%TZ)"
echo "== sha256"
# shellcheck disable=SC2086
sha256sum nunl/*.py nunl/*.sh $TESTS
echo "== ruff"
# shellcheck disable=SC2086
$RUFF check nunl $TESTS || fail=1
# shellcheck disable=SC2086
$RUFF format --check nunl $TESTS || fail=1
echo "== shellcheck"
if shellcheck nunl/*.sh; then echo "shellcheck clean"; else fail=1; fi
echo "== unit tests"
# shellcheck disable=SC2086
$PY -m pytest -q -rA -p no:cacheprovider $TESTS 2>&1 | grep -E '^(PASSED|FAILED|ERROR)|passed|failed' || true
# shellcheck disable=SC2086
$PY -m pytest -q -p no:cacheprovider $TESTS >/dev/null 2>&1 || { echo "unit tests FAIL"; fail=1; }

mutant() { # name file sed-expression: the suite must FAIL against the mutant
  local name=$1 file=$2 expr=$3 tmp
  tmp=$(mktemp -d)
  mkdir -p "$tmp/nunl" "$tmp/tests" "$tmp/pftstats"
  cp nunl/*.py "$tmp/nunl/"
  cp pftstats/__init__.py pftstats/stats.py "$tmp/pftstats/"
  # shellcheck disable=SC2086
  cp $TESTS "$tmp/tests/"
  sed -i "$expr" "$tmp/nunl/$file"
  if cmp -s "nunl/$file" "$tmp/nunl/$file"; then
    echo "MUTANT $name NOT APPLIED (sed matched nothing)"
    fail=1
  elif (cd "$tmp" && "$OLDPWD/$PY" -m pytest -q -p no:cacheprovider tests >"$tmp/out" 2>&1); then
    echo "MUTANT $name SURVIVED"
    fail=1
  else
    echo "MUTANT $name caught: $(grep -cE '^FAILED' "$tmp/out" || true) failing tests:" \
      "$(grep -oE '^FAILED [^ ]+' "$tmp/out" | sed 's/.*:://' | tr '\n' ' ')"
  fi
  rm -rf "$tmp"
}
echo "== mutants (thresholds)"
mutant T1-low-50-to-51pct analyze_nunl.py 's/^LOW = FLAG \* 50 \/\/ 100/LOW = FLAG * 51 \/\/ 100/'
mutant T2-high-80-to-79pct analyze_nunl.py 's/^HIGH = FLAG \* 80 \/\/ 100/HIGH = FLAG * 79 \/\/ 100/'
mutant T3-disable-lt-to-le analyze_nunl.py 's/    if score < LOW:/    if score <= LOW:/'
mutant T4-reenable-gt-to-ge analyze_nunl.py 's/return "REENABLE" if score > HIGH else/return "REENABLE" if score >= HIGH else/'
mutant T5-flag-interval-255 analyze_nunl.py 's/^FLAG = 256/FLAG = 255/'
mutant T6-min-local-85pct analyze_nunl.py 's/^MIN_LOCAL = FLAG \* 90 \/\/ 100/MIN_LOCAL = FLAG * 85 \/\/ 100/'
echo "== mutants (logic)"
mutant L1-no-list-transition-check analyze_nunl.py 's/            if exp != dis:/            if False:/'
mutant L2-no-next-ledger-check analyze_nunl.py 's/        if cur != nxt:/        if False:/'
mutant L3-risk-counts-wrong-hash analyze_nunl.py 's/            if chain.get(e\["li"\]) == e\["lh"\]:/            if e["li"] in chain:/'
mutant L4-risk-allows-incomplete-window analyze_nunl.py 's/        if all(li in chain for li in span):/        if True:/'
mutant L5-collector-no-retry-lgrNotFound collect_nunl.py 's/RETRY_ERRORS = {"lgrNotFound", /RETRY_ERRORS = {/'
mutant L6-flags-include-last collect_nunl.py 's/    return list(range(first, hi, FLAG))/    return list(range(first, hi + 1, FLAG))/'
mutant L7-crosscheck-ignores-nunl crosscheck_nunl.py 's/    for f in ("lh", "mods", "nunl", "next"):/    for f in ("lh", "mods", "next"):/'
mutant L8-crosscheck-no-positive-control crosscheck_nunl.py 's/    if not loc.get("amendments"):/    if False:/'
mutant L9-main-ignores-missing-flags analyze_nunl.py 's/ and not h\["missing"\] and / and /'
echo "== RESULT $([ $fail = 0 ] && echo PASS || echo FAIL)"
exit $fail
