#!/usr/bin/env bash
# govern offline checks: ruff, shellcheck, unit tests (fixtures and fakes only, no network), and
# mutants that the tests must catch. Run from the repo root: bash govern/check.sh
set -uo pipefail
PY=${PY:-.venv/bin/python}
RUFF=${RUFF:-.venv/bin/ruff}
TESTS="tests/test_govern_collect.py tests/test_govern_analyze.py tests/test_govern_crosscheck.py tests/test_govern_resume.py"
fail=0
echo "== OS: $(sed -n 's/^PRETTY_NAME="\(.*\)"/\1/p' /etc/os-release) | python: $($PY -V 2>&1) | $(date -u +%FT%TZ)"
echo "== sha256"
# shellcheck disable=SC2086
sha256sum govern/*.py govern/*.sh $TESTS
echo "== ruff"
# shellcheck disable=SC2086
$RUFF check govern $TESTS || fail=1
# shellcheck disable=SC2086
$RUFF format --check govern $TESTS || fail=1
echo "== shellcheck"
if shellcheck govern/*.sh; then echo "shellcheck clean"; else fail=1; fi
echo "== unit tests"
# shellcheck disable=SC2086
$PY -m pytest -q -rA -p no:cacheprovider $TESTS 2>&1 | grep -E '^(PASSED|FAILED|ERROR)|passed|failed' || true
# shellcheck disable=SC2086
$PY -m pytest -q -p no:cacheprovider $TESTS >/dev/null 2>&1 || { echo "unit tests FAIL"; fail=1; }

mutant() {  # name file sed-expression: the suite must FAIL against the mutant
  local name=$1 file=$2 expr=$3 tmp
  tmp=$(mktemp -d)
  mkdir -p "$tmp/govern" "$tmp/tests/fixtures" "$tmp/txflow" "$tmp/vlatency"
  cp govern/*.py "$tmp/govern/"
  cp txflow/wsclient.py "$tmp/txflow/"
  cp vlatency/collect_vlatency.py "$tmp/vlatency/"
  cp -r tests/fixtures/govern "$tmp/tests/fixtures/"
  # shellcheck disable=SC2086
  cp $TESTS "$tmp/tests/"
  sed -i "$expr" "$tmp/govern/$file"
  if cmp -s "govern/$file" "$tmp/govern/$file"; then
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
mutant M1-wrong-voting-ledger collect_govern.py 's/return (li + 1) % 256 == 0/return li % 256 == 0/'
mutant M2-collector-no-dedupe collect_govern.py 's/if (li, k, lh) in self.s.votes:/if False:/'
mutant M3-threshold-strict analyze_govern.py 's/c\[a\] \/ n_unl >= THRESHOLD/c[a] \/ n_unl > THRESHOLD/'
mutant M4-no-vote-changes analyze_govern.py 's/if prev is not None and cur != prev:/if False:/'
mutant M5-counts-losing-hash analyze_govern.py 's/if led_hash.get(e\["li"\]) not in (None, e\["lh"\]):/if False:/'
mutant M6-no-build-flag analyze_govern.py 's/"build_blocked_risk": 0 <= sv_key(sv) < newest/"build_blocked_risk": False/'
mutant M7-crosscheck-ignores-enabled crosscheck_govern.py 's/want = want_all - enabled if/want = want_all if/'
mutant M8-crosscheck-no-retry crosscheck_govern.py 's/default=4)/default=1)/'
mutant M9-checker-ignores-dup-votes gov_resume_check.py 's/vk and len(vk) == len(set(vk))/bool(vk)/'
mutant M10-checker-ignores-snapshots gov_resume_check.py 's/flags and set(snaps) == want and/bool(flags) and/'
mutant M11-preview-mid-line-cut analyze_govern.py 's/    head = md\[: md.rfind("\\n", 0, limit) + 1\]/    head = md[:limit]/'
echo "== RESULT $([ $fail = 0 ] && echo PASS || echo FAIL)"
exit $fail
