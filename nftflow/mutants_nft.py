#!/usr/bin/env python3
"""Mutation check for nftflow (reconciliation + crosscheck): put one broken line in at a time in a
temporary copy of the repo and run the nftflow tests; every mutant must be CAUGHT (tests fail).
Each mutant's text must occur exactly once, or it is reported NOT APPLIED (counts as a failure).

Usage: .venv/bin/python nftflow/mutants_nft.py   (from the repo root; exit 0 = all caught)
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

MUTANTS = [
    (
        "M1 mint of an already-live ID not flagged",
        "nftflow/reconcile_nft.py",
        "                if nid in live:\n",
        "                if False:\n",
    ),
    (
        "M2 burn owner not checked",
        "nftflow/reconcile_nft.py",
        '                if live.get(nid) != ob:\n                    residuals.append(f"R1 burn',
        '                if False:\n                    residuals.append(f"R1 burn',
    ),
    (
        "M3 transfer leaves the old owner in the replay",
        "nftflow/reconcile_nft.py",
        "not live with owner {ob} in {r['tx']['hash']}\")\n                live[nid] = oa\n",
        "not live with owner {ob} in {r['tx']['hash']}\")\n",
    ),
    (
        "M4 burns not subtracted in R2 issuer supply",
        "nftflow/reconcile_nft.py",
        "if la[iss] + mints[iss] - burns[iss] != lb[iss]:",
        "if la[iss] + mints[iss] != lb[iss] + 0 * burns[iss]:",
    ),
    (
        "M5 R3 ignores BurnedNFTokens",
        "nftflow/reconcile_nft.py",
        "        if db != burns[iss]:\n",
        "        if False:\n",
    ),
    (
        "M6 page split (same owner, other page) counted as transfer",
        "nftflow/reconcile_nft.py",
        "        elif ob != oa:\n",
        "        elif True:\n",
    ),
    (
        "M7 gaps.csv ignored",
        "nftflow/reconcile_nft.py",
        "        if n_gaps:\n",
        "        if False:\n",
    ),
    (
        "M8 duplicate tx hash (resume replay) not rejected",
        "nftflow/reconcile_nft.py",
        "    if twice:\n",
        "    if False:\n",
    ),
    (
        "M9 missing ledger in A+1..B not rejected",
        "nftflow/reconcile_nft.py",
        "    if missing:\n",
        "    if False:\n",
    ),
    (
        "M10 replay not in TransactionIndex order",
        "nftflow/reconcile_nft.py",
        'recs.sort(key=lambda r: (r["ledger_index"], r["tx_index"]))',
        'recs.sort(key=lambda r: (r["ledger_index"], -r["tx_index"]))',
    ),
    (
        "M11 deleted offers stay live in the replay",
        "nftflow/reconcile_nft.py",
        "            if offers.pop(idx, None) is None:\n",
        "            if offers.get(idx) is None:\n",
    ),
    (
        "M12 R5 misses an ID re-minted after A",
        "nftflow/reconcile_nft.py",
        "+ [n for n in minted_ids if n in own_a]",
        "+ []",
    ),
    (
        "M13 crosscheck X2 owner set check always passes",
        "nftflow/crosscheck_nft.py",
        "            got == exp,\n",
        "            True,\n",
    ),
    (
        "M14 crosscheck X1 ignores the server's NFTokenID",
        "nftflow/crosscheck_nft.py",
        "            and got == exp\n",
        "            and True\n",
    ),
    (
        "M15 crosscheck X4 checks only ledger B",
        "nftflow/crosscheck_nft.py",
        '((la, acc_a, "A"), (lb, acc_b, "B"))',
        '((lb, acc_b, "A"), (lb, acc_b, "B"))',
    ),
    (
        "M16 untouched tokens on a modified page counted as page moves",
        "nftflow/reconcile_nft.py",
        "        if pb is not None and pb == pa:\n",
        "        if False:\n",
    ),
    (
        "M17 report accepts a failed or --negative reconciliation",
        "nftflow/report_nft.py",
        "    if failed or negative:\n",
        "    if False:\n",
    ),
    (
        "M18 report prints full addresses",
        "nftflow/report_nft.py",
        '    return (a or "?")[:8] + "…"\n',
        '    return (a or "?") + "…"\n',
    ),
]
TESTS = ["tests/" + p.name for p in sorted(Path("tests").glob("test_nftflow_*.py"))]


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
