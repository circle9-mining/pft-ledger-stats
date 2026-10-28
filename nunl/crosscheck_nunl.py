#!/usr/bin/env python3
"""Crosscheck collected NegativeUNL history against your own node (read-only).

Picks N random flag ledgers from nunl.jsonl (seeded; the seed is printed) and re-reads each one from
the local postfiatd: ledger hash, UNLModify txs, NegativeUNL object at F (and at F+1 when the node
can get F+1: peers serve old flag ledgers quickly but usually not the ledger after one, so F+1 is
best effort, never counted as a failure and reported per ledger). The local node
keeps only ~512 ledgers (online_delete), so older ledgers are fetched first with the admin command
`ledger_request`, which acquires the ledger from peers and checks it against the hash in the node's
own validated skip list. Admin commands only answer on the container's loopback, so every call goes
through `docker exec <container> curl` (run this script with sudo rights for docker).

Positive control per ledger: the Amendments object must be readable at F (proves the local state
map is there, so "NegativeUNL absent" is a real answer, not a missing state tree).

--corrupt K mutates K of the chosen records before comparing (adds a fake disabled validator,
or flips the ledger hash): the run must then report MISMATCH and exit 1 (negative control).
Exit: 0 all OK, 1 any mismatch/unavailable.
"""

import argparse
import json
import random
import subprocess
import sys
import time
from datetime import datetime, timezone

here = __import__("os").path.dirname(__import__("os").path.abspath(__file__))
sys.path.insert(0, here)
from collect_nunl import NUNL_INDEX, nunl_view, unl_mods  # noqa: E402

AMENDMENTS_INDEX = "7DB0788C020F02780A673DC74757F23823FA3014C1866E72CC4CD8B226CD6EF4"
ALLOWED = {"ledger", "ledger_entry", "ledger_request"}
FAKE_KEY = "ED" + "AB" * 32


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg):
    print(f"{utcnow()} {msg}", flush=True)


class Local:
    def __init__(self, container, runner=None):
        self.container = container
        self.runner = runner or self._run
        self.calls = 0

    def _run(self, body):
        cmd = [
            "sudo",
            "-n",
            "docker",
            "exec",
            self.container,
            "timeout",
            "20",
            "curl",
            "-s",
            "-H",
            "Content-Type: application/json",
            "-d",
            body,
            "http://127.0.0.1:5005",
        ]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=40).stdout

    def call(self, method, params):
        if method not in ALLOWED:
            raise ValueError(f"refusing command {method}")
        self.calls += 1
        out = self.runner(json.dumps({"method": method, "params": [params]}))
        return json.loads(out)["result"]


def acquire(local, li, wait=180, sleep=time.sleep):
    """Make sure the local node holds validated ledger li. Returns (ok, attempts)."""
    deadline = time.monotonic() + wait
    n = 0
    while True:
        n += 1
        r = local.call("ledger", {"ledger_index": li})
        if r.get("validated") and r.get("ledger", {}).get("ledger_hash"):
            return True, n
        local.call("ledger_request", {"ledger_index": li})
        if time.monotonic() > deadline:
            return False, n
        sleep(3)


def read_local(local, li, with_next=True):
    led = local.call("ledger", {"ledger_index": li, "transactions": True, "expand": True})
    lg = led.get("ledger") or {}
    out = {
        "lh": lg.get("ledger_hash"),
        "mods": unl_mods(lg.get("transactions") or []),
        "validated": bool(led.get("validated")),
    }
    for name, at in (("nunl", li), ("next", li + 1))[: 2 if with_next else 1]:
        r = local.call("ledger_entry", {"index": NUNL_INDEX, "ledger_index": at})
        if r.get("error") == "entryNotFound":
            out[name] = None
        elif "node" in r:
            out[name] = nunl_view(r["node"])
        else:
            out[name] = {"error": r.get("error")}
    am = local.call("ledger_entry", {"index": AMENDMENTS_INDEX, "ledger_index": li})
    out["amendments"] = len((am.get("node") or {}).get("Amendments") or []) if "node" in am else None
    return out


def corrupt(rec, i):
    rec = json.loads(json.dumps(rec))
    if i % 2 == 0:
        v = rec.get("nunl") or {"disabled": [], "to_disable": None, "to_reenable": None}
        v["disabled"] = sorted(v["disabled"] + [FAKE_KEY])
        rec["nunl"] = v
        rec["_corruption"] = "fake disabled validator"
    else:
        rec["lh"] = rec["lh"][::-1]
        rec["_corruption"] = "ledger hash reversed"
    return rec


def compare(rec, loc):
    diffs = []
    for f in ("lh", "mods", "nunl", "next"):
        if f in loc and rec.get(f) != loc.get(f):
            diffs.append(f"{f}: dataset={json.dumps(rec.get(f))} local={json.dumps(loc.get(f))}")
    if not loc.get("validated"):
        diffs.append("local ledger not validated")
    if not loc.get("amendments"):
        diffs.append(f"positive control: Amendments object not readable locally ({loc.get('amendments')})")
    return diffs


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--hist", required=True)
    ap.add_argument("-n", type=int, default=10)
    ap.add_argument("--seed", type=int)
    ap.add_argument("--container", default="postfiatd")
    ap.add_argument("--wait", type=int, default=180, help="max seconds to acquire one ledger")
    ap.add_argument(
        "--wait-next",
        type=int,
        default=15,
        help="seconds to try F+1 (non-flag ledgers are usually not served by peers; best effort)",
    )
    ap.add_argument("--corrupt", type=int, default=0, help="negative control: corrupt K chosen records")
    args = ap.parse_args(argv)
    recs = {}
    with open(args.hist) as fh:
        for line in fh:
            e = json.loads(line)
            if e.get("ev") == "flag":
                recs[e["li"]] = e
    seed = args.seed if args.seed is not None else int(time.time())
    rng = random.Random(seed)
    pick = sorted(rng.sample(sorted(recs), min(args.n, len(recs))))
    log(
        f"crosscheck seed={seed} scanned_flags={len(recs)} range={min(recs)}-{max(recs)} picked={pick}"
        f" corrupt={args.corrupt}"
    )
    local = Local(args.container)
    ok = 0
    for i, li in enumerate(pick):
        rec = recs[li]
        if i < args.corrupt:
            rec = corrupt(rec, i)
            log(f"NEGATIVE CONTROL flag {li}: corrupted dataset record ({rec['_corruption']})")
        t0 = time.monotonic()
        got, n = acquire(local, li, args.wait)
        if not got:
            log(f"UNAVAILABLE flag {li}: local node could not acquire it within {args.wait}s (attempts={n})")
            continue
        nxt_ok, _ = acquire(local, li + 1, args.wait_next)
        loc = read_local(local, li, with_next=nxt_ok)
        diffs = compare(rec, loc)
        took = time.monotonic() - t0
        if diffs:
            log(f"MISMATCH flag {li} ({took:.0f}s)")
            for d in diffs:
                log(f"   {d}")
        else:
            ok += 1
            log(
                f"OK flag {li} hash={loc['lh'][:16]}... mods={len(loc['mods'])} nunl={loc['nunl']}"
                f" next={loc.get('next', 'n/a')}"
                f" amendments={loc['amendments']} acquire_attempts={n}"
                f" F+1={'checked' if nxt_ok else 'not acquirable locally (not compared)'} ({took:.0f}s)"
            )
    log(f"result {ok}/{len(pick)} OK local_calls={local.calls} -> {'PASS' if ok == len(pick) else 'FAIL'}")
    return 0 if ok == len(pick) else 1


if __name__ == "__main__":
    sys.exit(main())
