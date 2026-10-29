#!/usr/bin/env python3
"""Cross-check a diff.json / funders.json pair against the public RPC (read-only).

Checks, each printed as one OK/MISMATCH line (accounts truncated to 8 characters):
  1. --count random AccountRoots of snapshot B (seeded): account_info at ledger B must give
     the snapshot's Balance and OwnerCount.
  2. EVERY deleted account: account_info at ledger A gives the snapshot-A Balance, and at
     ledger B answers actNotFound.
  3. For each sampled account that was created in the window: `tx` of its creating hash
     (from funders.json) is validated, in the same ledger, from the same funder, and its
     metadata creates that account with the same Balance.

--negative is the control: expected Balances get +1 drop, so every balance check must
MISMATCH and the run must exit 1.

Exit 0 all OK, 1 any mismatch, 2 RPC failure.

Usage:
  ./crosscheck_churn.py diff.json funders.json snapB.jsonl.gz --count 20 --seed 1 [--negative] [--rpc URL]
"""

import argparse
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from diff_churn import load  # noqa: E402
from funders_churn import Rpc, RpcError, created_node  # noqa: E402


def short(a):
    return a[:8] + "…"


def account_info(rpc, address, ledger):
    """(Balance, OwnerCount) at `ledger`, or None for actNotFound."""
    try:
        res = rpc.call("account_info", {"account": address, "ledger_index": ledger})
    except RpcError as e:
        if "actNotFound" in str(e):
            return None
        raise
    d = res["account_data"]
    return int(d["Balance"]), d.get("OwnerCount", 0)


def run(diffdoc, fundoc, snap_b, rpc, count, seed, negative=False, out=print):
    bump = 1 if negative else 0
    la, lb = diffdoc["a"]["ledger_index"], diffdoc["b"]["ledger_index"]
    _, objs = load(snap_b)
    accts = sorted(
        (o for o in objs.values() if o.get("LedgerEntryType") == "AccountRoot"), key=lambda o: o["Account"]
    )
    sample = random.Random(seed).sample(accts, min(count, len(accts)))
    created = {r["address"]: r for r in fundoc["created"]}
    bad = checked = 0

    def report(ok, what):
        nonlocal bad, checked
        checked += 1
        bad += not ok
        out(f"{'OK' if ok else 'MISMATCH'} {what}")

    for o in sample:
        exp = (int(o["Balance"]) + bump, o.get("OwnerCount", 0))
        got = account_info(rpc, o["Account"], lb)
        report(got == exp, f"sample {short(o['Account'])} @B expected={exp} got={got}")
        c = created.get(o["Account"])
        if c:
            tx = rpc.call("tx", {"transaction": c["tx_hash"]})
            nf = created_node(tx.get("meta") or tx.get("meta_json") or {}, o["Account"])
            ok = (
                tx.get("validated") is True
                and tx.get("ledger_index") == c["ledger"]
                and (tx.get("Account") or tx.get("tx_json", {}).get("Account")) == c["funder"]
                and nf is not None
                and int(nf["Balance"]) == c["funded_drops"] + bump
            )
            report(ok, f"creating-tx {short(c['tx_hash'])} of {short(o['Account'])} ledger={c['ledger']}")
    for d in diffdoc["accounts"]["deleted"]:
        exp_a = (d["balance_drops"] + bump, d["owner_count"])
        got_a = account_info(rpc, d["address"], la)
        report(got_a == exp_a, f"deleted {short(d['address'])} @A expected={exp_a} got={got_a}")
        got_b = account_info(rpc, d["address"], lb)
        report(got_b is None, f"deleted {short(d['address'])} @B expected=actNotFound got={got_b}")
    out(
        f"SUMMARY checked={checked} mismatches={bad} sample={len(sample)}"
        f" deleted={len(diffdoc['accounts']['deleted'])}"
        f" negative={negative} rpc_calls={rpc.calls} retries={rpc.retried}"
    )
    return 1 if bad else 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("diff_json")
    p.add_argument("funders_json")
    p.add_argument("snap_b")
    p.add_argument("--count", type=int, default=20)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--negative", action="store_true")
    p.add_argument("--rpc", default="https://rpc.testnet.postfiat.org")
    p.add_argument("--gap", type=float, default=0.5)
    args = p.parse_args(argv)
    with open(args.diff_json) as fh:
        diffdoc = json.load(fh)
    with open(args.funders_json) as fh:
        fundoc = json.load(fh)
    try:
        return run(
            diffdoc, fundoc, args.snap_b, Rpc(args.rpc, gap=args.gap), args.count, args.seed, args.negative
        )
    except RpcError as e:
        print(f"ERROR {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
