"""Checks for resume_drill.sh: window kept, prefixes preserved, no duplicates/torn rows, gaps accounted.

Usage: resume_check.py <outdir> <snapdir>   (snapdir holds <stage>-<name>.csv copies taken at each interrupt)
A snapshot taken right after SIGKILL may end in a torn line or in tx rows of a ledger whose
ledgers.csv row was never written; the collector drops those on restart (and logs it), so the
same repair is applied to a copy of the snapshot before the prefix comparison, and printed.
Exit 0 only if every check passes.
"""
import csv
import os
import re
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from collect_tx import drop_orphan_tx, drop_torn_tail, last_index  # noqa: E402

NAMES = ("tx", "ledgers", "gaps")


def main(out, snap):
    fails = 0

    def check(ok, msg):
        nonlocal fails
        print(f"{'PASS' if ok else 'FAIL'} {msg}")
        fails += 0 if ok else 1

    log = open(os.path.join(out, "collect.log")).read()
    win = re.findall(r"WINDOW START (\S+) -> (\S+)", log)
    res = re.findall(r"RESUME at ledger \d+ \(window (\S+) -> (\S+),", log)
    check(len(win) == 1 and len(res) == 2 and all(r == win[0] for r in res),
          f"1 WINDOW START + 2 RESUME lines, same window: WINDOW={win} RESUME={res}")

    for stage in ("term", "kill"):
        with tempfile.TemporaryDirectory() as tmp:
            paths = {}
            for n in NAMES:
                src = os.path.join(snap, f"{stage}-{n}.csv")
                if os.path.exists(src):
                    paths[n] = shutil.copy(src, os.path.join(tmp, f"{n}.csv"))
            torn = {n: drop_torn_tail(p) for n, p in paths.items()}
            done = max(x for x in (last_index(paths.get("ledgers", "")), last_index(paths.get("gaps", "")),
                                   -1) if x is not None)
            orphans = drop_orphan_tx(paths["tx"], done) if "tx" in paths else 0
            print(f"INFO {stage} snapshot: torn bytes {torn}, orphan tx rows after ledger {done}: {orphans}")
            for n, p in paths.items():
                before = open(p, "rb").read()
                after = open(os.path.join(out, f"{n}.csv"), "rb").read()
                nb, na = before.count(b"\n") - 1, after.count(b"\n") - 1
                check(after.startswith(before),
                      f"{n}.csv at {stage} ({nb} rows) is a byte-identical prefix of final ({na} rows)")

    tables = {}
    for n in NAMES:
        path = os.path.join(out, f"{n}.csv")
        if not os.path.exists(path):
            tables[n] = []
            continue
        with open(path, newline="") as f:
            rows = list(csv.reader(f))
        width = len(rows[0])
        bad = [i + 1 for i, r in enumerate(rows) if len(r) != width]
        check(not bad, f"{n}.csv: all {len(rows)} lines have {width} columns (bad lines: {bad[:5]})")
        tables[n] = [dict(zip(rows[0], r, strict=True)) for r in rows[1:]]

    hashes = [r["tx_hash"] for r in tables["tx"]]
    check(len(hashes) == len(set(hashes)), f"tx: {len(hashes)} rows, {len(set(hashes))} unique tx_hash")
    led = [int(r["ledger_index"]) for r in tables["ledgers"]]
    check(len(led) == len(set(led)) and led == sorted(led),
          f"ledgers: {len(led)} rows, unique and increasing")
    gaps = {int(g["ledger_index"]) for g in tables["gaps"]}
    holes = set(range(led[0], led[-1] + 1)) - set(led)
    check(holes == gaps, f"ledgers {led[0]}-{led[-1]}: {len(holes)} missing, gaps.csv lists {len(gaps)}")
    per = {}
    for r in tables["tx"]:
        per[int(r["ledger_index"])] = per.get(int(r["ledger_index"]), 0) + 1
    bad = [r["ledger_index"] for r in tables["ledgers"]
           if int(r["txn_count"]) != per.get(int(r["ledger_index"]), 0)]
    check(not bad and set(per) <= set(led), f"txn_count matches tx.csv rows for all {len(led)} ledgers, "
          f"no tx rows outside ledgers.csv (bad: {bad[:5]})")
    print(f"RESULT: {'PASS' if not fails else f'FAIL ({fails})'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
