#!/usr/bin/env python3
"""Cross-check random ledgers from a vlatency vl.jsonl against the local node AND a public RPC.

For --count random ledgers (seeded) that the collector recorded as validated, fetches `ledger`
(read-only) from the local node's admin JSON-RPC and from the public RPC, and compares each answer's
ledger_hash and close_time with the collected values (any difference = FAIL). It also prints
close_time_resolution and close_flags, which the analyzer's close-time handling depends on.

Only ledgers the local node still holds can be checked there (online_delete keeps a few hundred),
so the sample is drawn from the collected ledgers inside the local complete_ledgers range; run it
during or right after a window. The public server sometimes gives transient incomplete answers
(lgrNotFound / internal / missing fields) for ledgers it holds; those are retried with exponential
backoff and every retry is printed. A ledger that still has no complete answer after --tries is a
FAIL, never skipped.

Exit 0 = every sampled ledger matches on both servers. Usage:
  crosscheck_vlatency.py vl.jsonl [--count 20] [--seed N] [--local URL] [--rpc URL] [--tries 4]
"""

import argparse
import json
import random
import sys
import time
import urllib.request

TRANSIENT = ("lgrNotFound", "internal", "noNetwork", "noCurrent", "tooBusy")


def http_rpc(url, method, params, timeout=20):
    body = json.dumps({"method": method, "params": [params]}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)["result"]


def fetch_ledger(fetch, url, li, tries=4, wait=2.0, sleep=time.sleep, out=None):
    """`ledger` for index li until it is complete (validated, hash, close_time); None after `tries`."""
    out = out or print
    for n in range(1, tries + 1):
        why = None
        try:
            r = fetch(url, "ledger", {"ledger_index": li})
            lg = r.get("ledger") or {}
            if r.get("error"):
                why = r["error"]
            elif not (r.get("validated") and lg.get("ledger_hash", lg.get("hash")) and "close_time" in lg):
                why = "incomplete answer"
            else:
                return r
        except Exception as ex:  # network errors are retried like transient RPC errors
            why = f"{type(ex).__name__}: {ex}"
        if n < tries:
            out(f"  retry {n}/{tries - 1} ledger {li} {url}: {why}; backoff {wait * 2 ** (n - 1):.0f}s")
            sleep(wait * 2 ** (n - 1))
        else:
            out(f"  giving up ledger {li} {url} after {tries} tries: {why}")
    return None


def load_ledgers(path):
    led = {}
    with open(path) as fh:
        for line in fh:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("ev") == "ledger":
                led.setdefault(e["li"], e)
    return led


def local_range(fetch, url):
    cl = fetch(url, "server_info", {})["info"]["complete_ledgers"]
    lo, hi = cl.split(",")[-1].split("-")  # newest contiguous range
    return int(lo), int(hi)


def main(argv=None, fetch=http_rpc, sleep=time.sleep):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("vl")
    ap.add_argument("--count", type=int, default=20)
    ap.add_argument("--seed", type=int, default=20261025)
    ap.add_argument("--local", default="http://127.0.0.1:5005")
    ap.add_argument("--rpc", default="https://rpc.testnet.postfiat.org")
    ap.add_argument("--tries", type=int, default=4)
    ap.add_argument("--wait", type=float, default=2.0)
    a = ap.parse_args(argv)
    led = load_ledgers(a.vl)
    lo, hi = local_range(fetch, a.local)
    pool = sorted(i for i in led if lo + 2 <= i <= hi)  # keep clear of the online_delete edge
    pick = sorted(random.Random(a.seed).sample(pool, min(a.count, len(pool))))
    print(
        f"crosscheck {len(pick)} of {len(led)} collected ledgers (seed {a.seed}; pool {len(pool)} inside"
        f" local complete_ledgers {lo}-{hi}) against local admin RPC and {a.rpc} `ledger`"
    )
    ok = 0
    for i, li in enumerate(pick):
        if i:
            sleep(1.0)
        mine = led[li]
        res = {}
        for name, url in (("local", a.local), ("public", a.rpc)):
            r = fetch_ledger(fetch, url, li, a.tries, a.wait, sleep)
            lg = (r or {}).get("ledger") or {}
            res[name] = (
                lg.get("ledger_hash") or lg.get("hash"),
                lg.get("close_time"),
                lg.get("close_time_resolution"),
                lg.get("close_flags"),
            )
        match = all(res[n][:2] == (mine["lh"], mine["ct"]) for n in res)
        ok += match
        print(
            f"{'MATCH' if match else 'MISMATCH'} ledger {li} collected hash={mine['lh']} ct={mine['ct']}"
            + "".join(
                f" | {n} hash={'=' if res[n][0] == mine['lh'] else res[n][0]}"
                f" ct={'=' if res[n][1] == mine['ct'] else res[n][1]} res={res[n][2]} flags={res[n][3]}"
                for n in res
            )
        )
    print(f"RESULT: {'PASS' if pick and ok == len(pick) else 'FAIL'} {ok}/{len(pick)} match")
    return 0 if pick and ok == len(pick) else 1


if __name__ == "__main__":
    sys.exit(main())
