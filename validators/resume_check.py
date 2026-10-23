"""Checks for resume_drill.sh: prefix preservation, duplicates, torn rows, gap accounting.

Usage: resume_check.py <outdir> <snapdir>   (snapdir holds <stage>-<name>.csv copies taken at each interrupt)
Exit 0 only if every check passes.
"""
import csv
import os
import re
import sys

NAMES = ("validations", "ledgers", "manifests", "gaps")


def main(out, snap):
    fails = 0

    def check(ok, msg):
        nonlocal fails
        print(f"{'PASS' if ok else 'FAIL'} {msg}")
        fails += 0 if ok else 1

    log = open(os.path.join(out, "collect.log")).read()
    win = re.findall(r"WINDOW start=(\S+) end=(\S+)", log)
    res = re.findall(r"RESUME window (\S+) -> (\S+)", log)
    check(len(win) == 1 and len(res) == 2 and all(r == win[0] for r in res),
          f"1 WINDOW + 2 RESUME lines, same window: WINDOW={win} RESUME={res}")

    for stage in ("term", "kill"):
        for n in NAMES:
            before = open(os.path.join(snap, f"{stage}-{n}.csv"), "rb").read()
            after = open(os.path.join(out, f"{n}.csv"), "rb").read()
            nb, na = before.count(b"\n") - 1, after.count(b"\n") - 1
            check(after.startswith(before),
                  f"{n}.csv at {stage} ({nb} rows) is a byte-identical prefix of final ({na} rows)")

    for n in NAMES:
        with open(os.path.join(out, f"{n}.csv"), newline="") as f:
            rows = list(csv.reader(f))
        width = len(rows[0])
        bad = [i + 1 for i, r in enumerate(rows) if len(r) != width]
        check(not bad, f"{n}.csv: all {len(rows)} lines have {width} columns (bad lines: {bad[:5]})")

    with open(os.path.join(out, "validations.csv"), newline="") as f:
        val = list(csv.DictReader(f))
    keys = [(r["ledger_index"], r["ledger_hash"], r["signing_key"]) for r in val]
    check(len(keys) == len(set(keys)), f"validations: {len(keys)} rows, {len(set(keys))} unique "
          f"(ledger_index, ledger_hash, signing_key)")

    with open(os.path.join(out, "ledgers.csv"), newline="") as f:
        led = [int(r["ledger_index"]) for r in csv.DictReader(f)]
    check(len(led) == len(set(led)) and led == sorted(led),
          f"ledgers: {len(led)} rows, unique and increasing")
    with open(os.path.join(out, "gaps.csv"), newline="") as f:
        gaps = [(int(g["first_missing"]), int(g["last_missing"])) for g in csv.DictReader(f)]
    missing = {i for a, b in gaps for i in range(a, b + 1)}
    holes = set(range(led[0], led[-1] + 1)) - set(led)
    check(holes == missing, f"ledgers {led[0]}-{led[-1]}: {len(holes)} missing, all recorded in gaps.csv "
          f"({len(gaps)} ranges, {len(missing)} ledgers)")
    print(f"RESULT: {'PASS' if not fails else f'FAIL ({fails})'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
