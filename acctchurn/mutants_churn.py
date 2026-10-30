#!/usr/bin/env python3
"""Mutation check for acctchurn: put one old/broken line back at a time in a temporary copy of the
repo and run the acctchurn tests; every mutant must be CAUGHT (tests fail). Each mutant's text must
occur exactly once, or it is reported NOT APPLIED (counts as a failure).

Usage: .venv/bin/python acctchurn/mutants_churn.py   (from the repo root; exit 0 = all caught)
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

MUTANTS = [
    (
        "M1 lgrNotFound/internal not retried",
        "acctchurn/funders_churn.py",
        'RETRY_ERRORS | {"lgrNotFound", "internal"}',
        "RETRY_ERRORS",
    ),
    (
        "M2 crosscheck samples every AccountRoot (old pool)",
        "acctchurn/crosscheck_churn.py",
        'pool = sorted({r["address"] for r in diffdoc["accounts"]["created"]} | set(changed))',
        "pool = sorted(roots)",
    ),
    (
        "M3 crosscheck @A check always passes",
        "acctchurn/crosscheck_churn.py",
        "            got_a == exp_a,",
        "            True,",
    ),
    (
        "M4 created accounts not counted in balance changes",
        "acctchurn/diff_churn.py",
        '[r["balance_drops"] for r in created]',
        "[]",
    ),
    (
        "M5 deleted accounts counted with the wrong sign",
        "acctchurn/diff_churn.py",
        '[-r["balance_drops"] for r in deleted]',
        '[r["balance_drops"] for r in deleted]',
    ),
    (
        "M6 feesum ignores txn_count",
        "acctchurn/feesum_churn.py",
        "if per_ledger[li][0] != ledgers[li][1]]",
        "if False]",
    ),
    # (dropping the `continue` after a duplicate is an equivalent mutant: txs is keyed by tx_hash,
    # so the identical row is just stored again; this mutant really keeps both rows)
    (
        "M7 feesum counts duplicate resume rows twice",
        "acctchurn/feesum_churn.py",
        "        txs[row[2]] = row\n",
        "        txs[row[2] + str(len(txs))] = row\n",
    ),
    (
        "M8 feesum accepts missing ledgers",
        "acctchurn/feesum_churn.py",
        "    if missing:\n",
        "    if False:\n",
    ),
    (
        "M9 report prints changed accounts untruncated",
        "acctchurn/report_churn.py",
        'out.append(f"| `{short(addr)}` |',
        'out.append(f"| `{addr}` |',
    ),
    (
        "M10 retry counter by error not kept",
        "rewardflow/collect_rewardflow.py",
        "self.retry_kinds[kind] = self.retry_kinds.get(kind, 0) + 1",
        "pass",
    ),
]
TESTS = ["tests/" + p.name for p in sorted(Path("tests").glob("test_acctchurn_*.py"))]


def pytest(root):
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *TESTS],
        cwd=root,
        capture_output=True,
        text=True,
    )
    last = (r.stdout.strip().splitlines() or ["?"])[-1]
    return r.returncode, last


def main():
    src = Path.cwd()
    rc, last = pytest(src)
    print(f"CONTROL unmutated: rc={rc} {last}")
    if rc != 0:
        return 1
    bad = 0
    for name, path, old, new in MUTANTS:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            shutil.copytree(src, root, ignore=shutil.ignore_patterns(".venv", ".git", "data", "__pycache__"))
            f = root / path
            text = f.read_text()
            if text.count(old) != 1:
                print(f"NOT APPLIED {name} (count {text.count(old)})")
                bad += 1
                continue
            f.write_text(text.replace(old, new))
            rc, last = pytest(root)
            caught = rc != 0
            bad += not caught
            print(f"{'CAUGHT' if caught else 'SURVIVED'} {name}: {last}")
    print(f"MUTANTS {len(MUTANTS)} caught={len(MUTANTS) - bad} failed={bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
