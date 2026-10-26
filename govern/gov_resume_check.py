#!/usr/bin/env python3
"""Checks for govern/resume_drill.sh. Usage: gov_resume_check.py FINAL TERM_SNAP KILL_SNAP; exit 0 = PASS.

`checks(final, term, kill)` takes the three files as bytes and returns [(name, ok, detail)], so the
checker itself is unit-tested on synthetic captures (tests/test_govern_resume.py).
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
        len(sess) == 3 and k != kc and sess[2].get("torn_bytes") == len(k) - len(kc),
        f"torn_bytes={sess[2].get('torn_bytes') if len(sess) == 3 else '?'}, expected {len(k) - len(kc)}",
    )
    stops = [e.get("reason") for e in evs if e.get("ev") == "stop"]
    check("stop events: SIGTERM then window complete", stops == ["SIGTERM", "window complete"], str(stops))

    votes = [e for e in evs if e.get("ev") == "vote"]
    vk = [(e["k"], e["li"], e["lh"]) for e in votes]
    check(
        "no duplicate vote entries (key, ledger index, ledger hash)",
        vk and len(vk) == len(set(vk)),
        f"{len(vk)} entries, {len(vk) - len(set(vk))} dup",
    )
    off = sorted({e["li"] for e in votes if (e["li"] + 1) % 256 != 0})
    check("every vote entry is for a voting ledger ((li+1) % 256 == 0)", vk and not off, f"off: {off}")

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

    flags = sorted(li for li in led if li % 256 == 0)
    snaps = [(e["li"], e["obj"]) for e in evs if e.get("ev") == "snap" and "node" in e]
    want = {(li, o) for li in flags for o in ("Amendments", "FeeSettings")}
    check(
        "every delivered flag ledger has exactly one Amendments and one FeeSettings snapshot",
        flags and set(snaps) == want and len(snaps) == len(set(snaps)),
        f"flag ledgers {flags}, {len(snaps)} snapshots",
    )
    voting_li = sorted({e["li"] for e in votes})
    check(
        "window covered a voting ledger and the flag ledger after it",
        any(li + 1 in flags for li in voting_li),
        f"voting ledgers with entries {voting_li}",
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
