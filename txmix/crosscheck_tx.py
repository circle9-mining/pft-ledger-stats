#!/usr/bin/env python3
"""Cross-check random collected ledgers against a fresh `ledger` RPC call.

Picks --count random ledgers from <data>/ledgers.csv that the node still holds
(online_delete keeps only the most recent ~512-1024 ledgers on light nodes),
re-fetches each with read-only `ledger` (transactions + expand), and compares:

  ledger_hash   collected vs fresh
  txn_count     ledgers.csv count vs fresh transaction count
  tx rows       tx.csv rows for that ledger vs fresh count
  tx hashes     set of tx.csv hashes vs fresh hashes

Exit 0 when every sampled ledger matches, 1 on any mismatch, 2 if too few
collected ledgers are still in the node's history. Reuses collect_tx.Rpc (read-only
allowlist, <= 1 request/second).

Usage:
  ./crosscheck_tx.py --data data/txmix --count 5 [--seed N]
"""

import argparse
import csv
import os
import random
import sys
from collections import defaultdict

from collect_tx import Rpc


def complete_range(info):
    """Highest contiguous a-b range from server_info complete_ledgers."""
    best = None
    for part in info["info"].get("complete_ledgers", "").split(","):
        if "-" in part:
            a, b = (int(x) for x in part.split("-"))
            if best is None or b > best[1]:
                best = (a, b)
    return best


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data", default="data/txmix")
    p.add_argument("--count", type=int, default=5)
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--container", default="postfiatd")
    p.add_argument("--port", type=int, default=5005)
    p.add_argument("--no-sudo", action="store_true")
    args = p.parse_args(argv)

    rpc = Rpc(args.container, args.port, 1.0, ["docker"] if args.no_sudo else ["sudo", "docker"])
    with open(os.path.join(args.data, "ledgers.csv"), newline="") as f:
        ledgers = {int(r["ledger_index"]): r for r in csv.DictReader(f)}
    tx_by = defaultdict(set)
    with open(os.path.join(args.data, "tx.csv"), newline="") as f:
        for r in csv.DictReader(f):
            tx_by[int(r["ledger_index"])].add(r["tx_hash"])

    lo, hi = complete_range(rpc.call("server_info"))
    held = sorted(i for i in ledgers if lo <= i <= hi)
    print(f"node complete range {lo}-{hi}; collected ledgers still held: {len(held)} of {len(ledgers)}")
    if len(held) < args.count:
        print("too few collected ledgers in node history", file=sys.stderr)
        return 2
    # prefer ledgers with transactions so the tx-hash comparison is meaningful
    with_tx = [i for i in held if int(ledgers[i]["txn_count"]) > 0]
    pool = with_tx if len(with_tx) >= args.count else held
    rng = random.Random(args.seed)
    sample = sorted(rng.sample(pool, args.count))

    bad = 0
    print("ledger_index,hash_match,csv_txn_count,fresh_txn_count,tx_rows,tx_hashes_match,result")
    for i in sample:
        fresh = rpc.call("ledger", {"ledger_index": i, "transactions": True, "expand": True})["ledger"]
        fh = {t.get("hash") for t in fresh.get("transactions") or []}
        ok_hash = fresh["ledger_hash"] == ledgers[i]["ledger_hash"]
        ok_n = int(ledgers[i]["txn_count"]) == len(fh)
        ok_rows = len(tx_by[i]) == len(fh)
        ok_set = tx_by[i] == fh
        ok = ok_hash and ok_n and ok_rows and ok_set
        bad += not ok
        print(f"{i},{ok_hash},{ledgers[i]['txn_count']},{len(fh)},{len(tx_by[i])},{ok_set},"
              f"{'MATCH' if ok else 'MISMATCH'}")
    print(f"CROSSCHECK {'PASS' if not bad else 'FAIL'}: {args.count - bad}/{args.count} ledgers match")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
