#!/usr/bin/env python3
"""Checks for vlatency/resume_drill.sh. Usage: vl_resume_check.py FINAL TERM_SNAP KILL_SNAP; exit 0 = PASS.

`checks(final, term, kill)` takes the three files as bytes and returns [(name, ok, detail)], so the
checker itself is unit-tested on synthetic captures (tests/test_vlatency_resume.py).
"""

import json
import sys


def checks(data, t, k):
    res = []

    def check(name, ok, detail=""):
        res.append((name, bool(ok), detail))

    check("SIGTERM snapshot ends with a newline (clean stop)", t.endswith(b"\n"))
    check(
        "SIGTERM snapshot is a byte-identical prefix of the final file", data.startswith(t), f"{len(t)} bytes"
    )
    kc = k[: k.rfind(b"\n") + 1]
    check(
        "SIGKILL snapshot (complete lines) is a byte-identical prefix of the final file",
        len(kc) > len(t) and data.startswith(kc),
        f"{len(kc)} bytes, torn tail {len(k) - len(kc)} bytes",
    )
    evs, bad = [], 0
    for line in data.splitlines():
        try:
            evs.append(json.loads(line))
        except ValueError:
            bad += 1
    check("every line parses as JSON", bad == 0 and evs, f"{len(evs)} lines, {bad} bad")
    sess = [e for e in evs if e.get("ev") == "session"]
    check(
        "3 sessions: resumed false, true, true",
        [s.get("resumed") for s in sess] == [False, True, True],
        str([s.get("resumed") for s in sess]),
    )
    check("same window_end in every session", len(sess) == 3 and len({s["window_end"] for s in sess}) == 1)
    check(
        "session 3 records the torn bytes cut after SIGKILL",
        len(sess) == 3 and sess[2].get("torn_bytes") == len(k) - len(kc),
        f"torn_bytes={sess[2].get('torn_bytes') if len(sess) == 3 else '?'}",
    )
    stops = [e.get("reason") for e in evs if e.get("ev") == "stop"]
    check("stop events: SIGTERM then window complete", stops == ["SIGTERM", "window complete"], str(stops))
    vals = [(e["k"], e["li"], e["lh"]) for e in evs if e.get("ev") == "val"]
    check(
        "no duplicate validation entries (key, ledger index, ledger hash)",
        vals and len(vals) == len(set(vals)),
        f"{len(vals)} entries, {len(vals) - len(set(vals))} dup",
    )
    led = [e["li"] for e in evs if e.get("ev") == "ledger"]
    check("no duplicate ledger entries", led and len(led) == len(set(led)), f"{len(led)} entries")
    gaps = [e for e in evs if e.get("ev") == "gap"]
    if led:
        led = sorted(led)
        covered = {i for g in gaps for i in range(g["from"], g["to"] + 1)}
        missing = set(range(led[0], led[-1] + 1)) - set(led)
        check(
            "ledgers contiguous apart from recorded gaps",
            missing == covered and all(g["count"] == g["to"] - g["from"] + 1 for g in gaps),
            f"{led[0]}-{led[-1]}, {len(led)} ledgers, gaps "
            + (", ".join(f"{g['from']}-{g['to']} ({g['count']}, {g['reason']})" for g in gaps) or "none"),
        )
    else:
        check("ledgers contiguous apart from recorded gaps", False, "no ledgers")
    check(
        "two gaps, both labelled restart",
        [g.get("reason") for g in gaps] == ["restart", "restart"],
        str([g.get("reason") for g in gaps]),
    )
    return res


def main(final, term, kill):
    def rd(p):
        with open(p, "rb") as fh:
            return fh.read()

    res = checks(rd(final), rd(term), rd(kill))
    for name, ok, detail in res:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}{(' - ' + detail) if detail else ''}")
    n = sum(ok for _, ok, _ in res)
    print(f"RESULT: {'PASS' if n == len(res) else 'FAIL'} ({n}/{len(res)})")
    return 0 if n == len(res) else 1


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:4]))
