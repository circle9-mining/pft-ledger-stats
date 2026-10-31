#!/usr/bin/env python3
"""Mutation check for keyposture (reconcile, posture, collector, count, crosscheck, stats): put one
broken line in at a time in a
temporary copy of the repo and run the keyposture tests; every mutant must be CAUGHT (tests fail).
Each mutant's text must occur exactly once, or it is reported NOT APPLIED (counts as a failure).

Usage: .venv/bin/python keyposture/mutants_key.py   (from the repo root; exit 0 = all caught)
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

MUTANTS = [
    (
        "K1 created AccountRoot already live not flagged",
        "keyposture/reconcile_key.py",
        "                    if a in cur:\n",
        "                    if False:\n",
    ),
    (
        "K2 previous RegularKey not compared",
        "keyposture/reconcile_key.py",
        'if "RegularKey" in prev and prev["RegularKey"] != cur[a]["regular_key"]:',
        "if False:",
    ),
    (
        "K3 previous master flag not compared",
        "keyposture/reconcile_key.py",
        'if "Flags" in prev and (not (int(prev["Flags"]) & LSF_DISABLE_MASTER)) != cur[a]["master"]:',
        "if False:",
    ),
    (
        "K4 unchanged-field RegularKey check dropped",
        "keyposture/reconcile_key.py",
        'if "RegularKey" not in prev and cur[a]["regular_key"] not in (None, f.get("RegularKey")):',
        "if False:",
    ),
    (
        "K5 deleted account kept in replay",
        "keyposture/reconcile_key.py",
        "                    del cur[a]\n",
        "                    pass\n",
    ),
    (
        "K6 SignerList deletion not applied",
        "keyposture/reconcile_key.py",
        '                    cur[a]["signers"] = None\n',
        "                    pass\n",
    ),
    (
        "K7 metadata node order not fixed (AccountDelete)",
        "keyposture/reconcile_key.py",
        "sorted(out, key=lambda t: t[0])",
        "out",
    ),
    (
        "K8 replay in file order, not (ledger, TransactionIndex)",
        "keyposture/reconcile_key.py",
        '    lines.sort(key=lambda x: (int(x["ledger_index"]), int(x["tx_index"])))\n',
        "    pass\n",
    ),
    (
        "K9 R1 posture comparison dropped",
        "keyposture/reconcile_key.py",
        "        elif cur[a] != pb[a]:\n",
        "        elif False:\n",
    ),
    (
        "K10 R2 outflow not subtracted",
        "keyposture/reconcile_key.py",
        "exp = ca.get(c, 0) + inflow - outflow",
        "exp = ca.get(c, 0) + inflow",
    ),
    (
        "K11 R3 SignerList set not compared",
        "keyposture/reconcile_key.py",
        "    if sl_idx != set(slb):\n",
        "    if False:\n",
    ),
    ("K12 gaps.csv ignored", "keyposture/reconcile_key.py", "        if g:\n", "        if False:\n"),
    ("K13 key_tx_count mismatch ignored", "keyposture/reconcile_key.py", "    if bad:\n", "    if False:\n"),
    ("K14 duplicate tx hash not rejected", "keyposture/reconcile_key.py", "    if d:\n", "    if False:\n"),
    (
        "K15 AccountRoot keylet self-test dropped",
        "keyposture/reconcile_key.py",
        'if account_keylet(o["Account"]) != o["index"]:',
        "if False:",
    ),
    (
        "K16 count file ok flag ignored",
        "keyposture/reconcile_key.py",
        '    if not c.get("ok"):\n',
        "    if False:\n",
    ),
    ("K17 SignerList keylet space byte wrong", "keyposture/posture_key.py", 'b"\\x00\\x53"', 'b"\\x00\\x54"'),
    (
        "K18 single-signer-quorum uses > instead of >=",
        "keyposture/posture_key.py",
        'max(weights) >= ms["quorum"]',
        'max(weights) > ms["quorum"]',
    ),
    (
        "K19 blackhole regular key treated as usable",
        "keyposture/posture_key.py",
        "usable_rk = bool(rk) and rk not in BLACKHOLES",
        "usable_rk = bool(rk)",
    ),
    (
        "K20 collector ignores lsfDisableMaster flips",
        "keyposture/collect_key.py",
        '(int(prev["Flags"]) ^ int(fin.get("Flags", 0))) & LSF_DISABLE_MASTER',
        "False",
    ),
    (
        "K21 collector ignores RegularKey changes",
        "keyposture/collect_key.py",
        '                if "RegularKey" in prev:\n',
        "                if False:\n",
    ),
    (
        "K22 count_key misses only-in-snapshot indexes",
        "keyposture/count_key.py",
        "only_s, only_p = sorted(s - set(p)), sorted(set(p) - s)",
        "only_s, only_p = [], sorted(set(p) - s)",
    ),
    (
        "K23 crosscheck tx result not checked",
        "keyposture/crosscheck_key.py",
        'and meta.get("TransactionResult") == "tesSUCCESS"',
        "and True",
    ),
    (
        "K24 crosscheck posture comparison always OK",
        "keyposture/crosscheck_key.py",
        "report(got == want, ",
        "report(True, ",
    ),
    (
        "K25 stats accepts a --negative reconciliation",
        "keyposture/stats_key.py",
        'if res.get("result") != "PASS" or res.get("negative"):',
        'if res.get("result") != "PASS":',
    ),
    (
        "K26 count_key --offline ignores the snapshot file count",
        "keyposture/count_key.py",
        'good = n == r["snapshot"] == r["paged"] and diffs == 0',
        'good = r["snapshot"] == r["paged"] and diffs == 0',
    ),
    (
        "K27 count_key --offline ignores recorded differences",
        "keyposture/count_key.py",
        'and diffs == 0 and r["ok"] is True',
        'and r["ok"] is True',
    ),
    (
        "K28 count_key --offline accepts a broken marker chain",
        "keyposture/count_key.py",
        '        if page["marker_in"] != prev:\n',
        "        if False:\n",
    ),
    (
        "K29 count_key --offline accepts a count file for another ledger",
        "keyposture/count_key.py",
        'if (rec["ledger_index"], rec["ledger_hash"]) != (',
        'if False and (rec["ledger_index"], rec["ledger_hash"]) != (',
    ),
]
TESTS = ["tests/" + p.name for p in sorted(Path("tests").glob("test_keyposture_*.py"))]


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
