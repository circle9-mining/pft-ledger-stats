#!/usr/bin/env python3
"""Find the creating transaction of every account created between snapshots A and B, and
build the funding graph (read-only public RPC: account_tx, account_info only).

Input is the diff.json written by diff.py. For each created AccountRoot, account_tx
(forward, ledgers A+1..B) is paged until the transaction whose metadata holds a CreatedNode
AccountRoot for that address. That transaction gives: funder (tx Account), tx type, ledger,
hash, and the native amount the new account received (its Balance in the CreatedNode).

Graph output (JSON, sorted keys, deterministic for the same RPC answers):
  - created[]: address, funder, tx_hash, tx_type, ledger, funded_drops, reserve_multiple
    (funded / reserve_base from diff.json snapshot B), active (sent >= 1 tx: Sequence in B
    differs from the Sequence it was created with), deleted_again (not in B)
  - funders[]: funder, accounts funded, total drops, share of all created accounts
  - chains[]: created accounts that themselves funded another created account (depth >= 2)
  - not_found[]: addresses whose creating tx was not found in the window (should be empty)
  - rpc: calls, retries

Exit 0 when every created account was resolved, 1 if any is in not_found, 2 on RPC failure.

Usage:
  ./funders.py diff.json --reserve-base 10000000 [--rpc URL] [--out funders.json]
"""

import argparse
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "rewardflow"))
from collect_rewardflow import Rpc, RpcError  # noqa: E402


def created_node(meta, address):
    """The CreatedNode AccountRoot NewFields for `address` in this tx's metadata, or None."""
    for node in meta.get("AffectedNodes", []):
        c = node.get("CreatedNode")
        if (
            c
            and c.get("LedgerEntryType") == "AccountRoot"
            and c.get("NewFields", {}).get("Account") == address
        ):
            return c["NewFields"]
    return None


def find_creation(rpc, address, lo, hi, limit=50):
    """Creating tx of `address` inside ledgers lo..hi, or None.

    The window is enforced HERE, not trusted to the server: the public RPC (1.0.9) echoes
    ledger_index_min/max but returned transactions from before ledger_index_min (runlog 2).
    """
    marker = None
    while True:
        params = {
            "account": address,
            "ledger_index_min": lo,
            "ledger_index_max": hi,
            "forward": True,
            "limit": limit,
        }
        if marker:
            params["marker"] = marker
        res = rpc.call("account_tx", params)
        for entry in res.get("transactions", []):
            tx = entry.get("tx") or entry.get("tx_json") or {}
            meta = entry.get("meta") or {}
            ledger = tx.get("ledger_index") or entry.get("ledger_index")
            if ledger is None or ledger < lo:
                continue
            if ledger > hi:
                return None  # forward order: nothing later can be inside the window
            nf = created_node(meta, address)
            if nf is not None:
                return {
                    "funder": tx.get("Account"),
                    "tx_hash": tx.get("hash") or entry.get("hash"),
                    "tx_type": tx.get("TransactionType"),
                    "ledger": tx.get("ledger_index") or entry.get("ledger_index"),
                    "funded_drops": int(nf.get("Balance", "0")),
                    "created_sequence": nf.get("Sequence"),
                }
        marker = res.get("marker")
        if not marker:
            return None


def build(diffdoc, rpc, reserve_base):
    lo, hi = diffdoc["a"]["ledger_index"] + 1, diffdoc["b"]["ledger_index"]
    created, not_found = [], []
    for row in diffdoc["accounts"]["created"]:
        found = find_creation(rpc, row["address"], lo, hi)
        if found is None:
            not_found.append(row["address"])
            continue
        found.update(
            {
                "address": row["address"],
                "reserve_multiple": round(found["funded_drops"] / reserve_base, 3),
                "active": row["sequence"] != found["created_sequence"],
            }
        )
        created.append(found)
    created.sort(key=lambda r: (r["ledger"], r["address"]))

    per = defaultdict(lambda: {"accounts": 0, "drops": 0})
    for r in created:
        per[r["funder"]]["accounts"] += 1
        per[r["funder"]]["drops"] += r["funded_drops"]
    n = len(created)
    funders = sorted(
        (
            {
                "funder": f,
                "accounts": v["accounts"],
                "drops": v["drops"],
                "share": round(v["accounts"] / n, 4) if n else 0.0,
            }
            for f, v in per.items()
        ),
        key=lambda r: (-r["accounts"], r["funder"]),
    )
    new = {r["address"]: r for r in created}
    chains = []
    for r in created:
        depth, cur, seen = 1, r, {r["address"]}
        while cur["funder"] in new and cur["funder"] not in seen:
            seen.add(cur["funder"])
            cur = new[cur["funder"]]
            depth += 1
        if depth >= 2:
            chains.append({"address": r["address"], "depth": depth, "root_funder": cur["funder"]})
    return {
        "window": {"ledger_min": lo, "ledger_max": hi},
        "created": created,
        "funders": funders,
        "chains": chains,
        "not_found": sorted(not_found),
        "never_active": sum(1 for r in created if not r["active"]),
        "rpc": {"calls": rpc.calls, "retries": rpc.retried},
    }


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("diff_json")
    p.add_argument("--reserve-base", type=int, required=True, help="drops, from snapshot B FeeSettings")
    p.add_argument("--rpc", default="https://rpc.testnet.postfiat.org")
    p.add_argument("--gap", type=float, default=0.5)
    p.add_argument("--out")
    args = p.parse_args(argv)
    with open(args.diff_json) as fh:
        diffdoc = json.load(fh)
    try:
        result = build(diffdoc, Rpc(args.rpc, gap=args.gap), args.reserve_base)
    except RpcError as e:
        print(f"ERROR {e}", file=sys.stderr)
        return 2
    text = json.dumps(result, sort_keys=True, indent=1) + "\n"
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text)
    print(
        f"created={len(result['created'])} not_found={len(result['not_found'])} "
        f"funders={len(result['funders'])} chains={len(result['chains'])} rpc={result['rpc']}",
        file=sys.stderr,
    )
    return 1 if result["not_found"] else 0


if __name__ == "__main__":
    sys.exit(main())
