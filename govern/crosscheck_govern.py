#!/usr/bin/env python3
"""Cross-check amendment majorities recomputed from raw validations against a public Post Fiat RPC.

For --count random flag ledgers F (seeded) whose voting ledger F-1 and next flag ledger F+256 are both
inside the capture, recompute from the raw `vote` entries of voting ledger F-1 which amendments reached
a UNL yes-share >= 80% (UNL = unl-start.json). The vote is tallied when F closes and its pseudo-
transactions land in F+1, so the public `ledger_entry` Amendments object at the NEXT flag ledger F+256
(before the following vote lands in F+257) must hold exactly those amendments in Majorities, minus any
already enabled. Read-only, >= 1 s between requests.

The public server sometimes gives transient incomplete answers (lgrNotFound, "internal") that succeed
seconds later. A non-matching answer is retried (--tries in total, backoff 2, 4, 8 s ...), every retry
is printed, and only the final answer counts: a mismatch that persists is still a MISMATCH.

Usage: crosscheck_govern.py DATA_DIR [--count 20] [--seed N] [--rpc URL]   (exit 0 = all match)
"""

import argparse
import json
import os
import random
import sys
import time
import urllib.request

AMENDMENTS_ID = "7DB0788C020F02780A673DC74757F23823FA3014C1866E72CC4CD8B226CD6EF4"
THRESHOLD = 0.8


def rpc_amendments(url, li, timeout=20):
    body = json.dumps(
        {"method": "ledger_entry", "params": [{"index": AMENDMENTS_ID, "ledger_index": li}]}
    ).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)["result"]


def load(data):
    with open(os.path.join(data, "unl-start.json")) as fh:
        u = json.load(fh)
    unl = set(u["trusted_validator_keys"])
    signing = {sk: mk for mk, sk in (u.get("signing_keys") or {}).items()}
    ledgers, votes = {}, {}
    with open(os.path.join(data, "govern.jsonl")) as fh:
        for line in fh:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("ev") == "ledger":
                ledgers[e["li"]] = e.get("lh")
            elif e.get("ev") == "vote":
                votes.setdefault(e["li"], []).append(e)
    return unl, signing, ledgers, votes


def expected(unl, signing, lh, entries):
    """Amendments with UNL yes-share >= 80% on one voting ledger.

    One vote per UNL master key, validations of the validated hash only."""
    by_v = {}
    for e in entries:
        v = e.get("mk") or signing.get(e["k"])
        if v in unl and e["lh"] == lh:
            by_v[v] = e
    yes = {}
    for e in by_v.values():
        for a in e.get("am") or []:
            yes[a] = yes.get(a, 0) + 1
    return {a for a, n in yes.items() if n / len(unl) >= THRESHOLD}, len(by_v)


def main(argv=None, fetch=rpc_amendments, sleep=time.sleep):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("data")
    ap.add_argument("--count", type=int, default=20)
    ap.add_argument("--seed", type=int, default=20261026)
    ap.add_argument("--rpc", default="https://rpc.testnet.postfiat.org")
    ap.add_argument("--tries", type=int, default=4)
    a = ap.parse_args(argv)
    unl, signing, ledgers, votes = load(a.data)
    flags = sorted(
        f for f in ledgers if f % 256 == 0 and f - 1 in ledgers and f - 1 in votes and f + 256 in ledgers
    )
    pick = sorted(random.Random(a.seed).sample(flags, min(a.count, len(flags))))
    print(
        f"crosscheck {len(pick)} of {len(flags)} eligible flag ledgers (seed {a.seed}), UNL {len(unl)},"
        f" threshold {THRESHOLD:.0%}, against {a.rpc} ledger_entry Amendments at F+256"
    )
    ok = 0
    for i, f in enumerate(pick):
        if i:
            sleep(1.0)
        want_all, n_val = expected(unl, signing, ledgers[f - 1], votes[f - 1])
        for attempt in range(1, a.tries + 1):
            err, enabled, majs = None, None, None
            try:
                r = fetch(a.rpc, f + 256)
                node = r.get("node") if r.get("status") == "success" and r.get("validated") else None
                err = r.get("error")
                if node is not None:
                    enabled = set(node.get("Amendments") or [])
                    majs = {m["Majority"]["Amendment"] for m in node.get("Majorities") or []}
                elif not err:
                    err = "incomplete answer"
            except Exception as ex:  # network errors count as mismatch, never skipped silently
                err = f"{type(ex).__name__}: {ex}"
            want = want_all - enabled if enabled is not None else None
            match = majs is not None and majs == want
            if match or attempt == a.tries:
                break
            wait = 2.0**attempt
            print(
                f"RETRY {attempt}/{a.tries - 1} flag {f} next {f + 256}: error={err} -> again in {wait:.0f}s"
            )
            sleep(wait)
        ok += match
        print(
            f"{'MATCH' if match else 'MISMATCH'} flag {f}: voting ledger {f - 1}"
            f" UNL validations {n_val}/{len(unl)},"
            f" recomputed majorities {sorted(want_all)}"
            f" | public {f + 256}: majorities {sorted(majs) if majs is not None else '-'},"
            f" enabled {len(enabled) if enabled is not None else '-'}{' error=' + err if err else ''}"
        )
    print(f"RESULT: {ok}/{len(pick)} match")
    return 0 if pick and ok == len(pick) else 1


if __name__ == "__main__":
    sys.exit(main())
