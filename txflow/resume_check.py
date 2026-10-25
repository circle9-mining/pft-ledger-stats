#!/usr/bin/env python3
"""Checks for resume_drill.sh: resume_check.py FINAL TERM_SNAPSHOT KILL_SNAPSHOT. Exit 0 = all PASS."""

import json
import sys


def main(final, term, kill):
    res = []

    def check(name, ok, detail=""):
        res.append(ok)
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}{(' - ' + detail) if detail else ''}")

    data = open(final, "rb").read()
    t = open(term, "rb").read()
    k = open(kill, "rb").read()
    check("SIGTERM snapshot ends with a newline (clean stop)", t.endswith(b"\n"))
    check(
        "SIGTERM snapshot is a byte-identical prefix of the final file", data.startswith(t), f"{len(t)} bytes"
    )
    kc = k[: k.rfind(b"\n") + 1]
    check(
        "SIGKILL snapshot (complete lines) is a byte-identical prefix of the final file",
        data.startswith(kc),
        f"{len(kc)} bytes, torn tail {len(k) - len(kc)} bytes",
    )
    evs, bad = [], 0
    for line in data.splitlines():
        try:
            evs.append(json.loads(line))
        except ValueError:
            bad += 1
    check("every line parses as JSON", bad == 0, f"{len(evs)} lines, {bad} bad")
    sess = [e for e in evs if e["ev"] == "session"]
    check(
        "3 sessions: resumed false, true, true",
        [s["resumed"] for s in sess] == [False, True, True],
        str([s["resumed"] for s in sess]),
    )
    check("same window_end in every session", len({s["window_end"] for s in sess}) == 1)
    check(
        "session 3 records the torn bytes cut after SIGKILL",
        len(sess) == 3 and sess[2]["torn_bytes"] == len(k) - len(kc),
        f"torn_bytes={sess[2]['torn_bytes'] if len(sess) == 3 else '?'}",
    )
    stops = [e["reason"] for e in evs if e["ev"] == "stop"]
    check("stop events: SIGTERM then window complete", stops == ["SIGTERM", "window complete"], str(stops))
    for kind, key in (("ledger", "ledger_index"), ("proposed", "hash"), ("validated", "hash")):
        vals = [e[key] for e in evs if e["ev"] == kind]
        check(f"no duplicate {kind} entries", len(vals) == len(set(vals)), f"{len(vals)} entries")
    led = sorted(e["ledger_index"] for e in evs if e["ev"] == "ledger")
    gaps = [e for e in evs if e["ev"] == "gap"]
    covered = {i for g in gaps for i in range(g["from"], g["to"] + 1)}
    missing = {i for i in range(led[0], led[-1] + 1)} - set(led)
    check(
        "ledgers contiguous apart from recorded gaps",
        missing == covered,
        f"{led[0]}-{led[-1]}, {len(led)} ledgers, gaps "
        + ", ".join(f"{g['from']}-{g['to']} ({g['count']}, {g['reason']})" for g in gaps),
    )
    check("both gaps labelled restart", [g["reason"] for g in gaps] == ["restart", "restart"])
    print(f"RESULT: {'PASS' if all(res) else 'FAIL'} ({sum(res)}/{len(res)})")
    return 0 if all(res) else 1


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:4]))
