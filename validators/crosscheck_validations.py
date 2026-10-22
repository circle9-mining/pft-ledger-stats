#!/usr/bin/env python3
"""Cross-check random collected ledgers against a fresh `ledger` RPC call.

Picks --count random ledgers from <data>/ledgers.csv that the node still holds
(online_delete keeps only the most recent ~512-1024 ledgers on light nodes),
re-fetches each with read-only `ledger`, and compares:

  ledger_hash    ledgers.csv vs fresh
  close_time     ledgers.csv ledger_time vs fresh close_time
  txn_count      ledgers.csv vs fresh transaction count
  validations    validations.csv rows for that ledger whose ledger_hash equals the
                 fresh hash (must be >= 1: someone validated the canonical ledger)

Exit 0 when every sampled ledger matches, 1 on any mismatch, 2 if too few
collected ledgers are still in the node's history. Reuses txmix/collect_tx.Rpc
(read-only allowlist, <= 1 request/second).

Usage:
  ./crosscheck_validations.py --data data/validators --count 5 [--seed N]
"""

import argparse
import csv
import os
import random
import sys
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "txmix"))

from collect_tx import Rpc  # noqa: E402
from crosscheck_tx import complete_range  # noqa: E402


def compare(row, canonical_votes, fresh):
    """Return (ok, fields) for one collected ledger row vs a fresh `ledger` result."""
    fresh_n = len(fresh.get("transactions") or [])
    fields = {
        "hash_match": fresh["ledger_hash"] == row["ledger_hash"],
        "close_time_match": int(fresh["close_time"]) == int(row["ledger_time"]),
        "txn_count_match": fresh_n == int(row["txn_count"]),
        "canonical_validations": canonical_votes,
    }
    ok = all(fields[k] for k in ("hash_match", "close_time_match", "txn_count_match")) and canonical_votes > 0
    return ok, fields


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data", default="data/validators")
    p.add_argument("--count", type=int, default=5)
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--container", default="postfiatd")
    p.add_argument("--port", type=int, default=5005)
    p.add_argument("--no-sudo", action="store_true")
    args = p.parse_args(argv)

    rpc = Rpc(args.container, args.port, 1.0, ["docker"] if args.no_sudo else ["sudo", "docker"])
    with open(os.path.join(args.data, "ledgers.csv"), newline="") as f:
        ledgers = {int(r["ledger_index"]): r for r in csv.DictReader(f)}

    lo, hi = complete_range(rpc.call("server_info"))
    held = sorted(i for i in ledgers if lo <= i <= hi)
    print(f"node complete range {lo}-{hi}; collected ledgers still held: {len(held)} of {len(ledgers)}")
    if len(held) < args.count:
        print("too few collected ledgers in node history", file=sys.stderr)
        return 2
    rng = random.Random(args.seed)
    sample = sorted(rng.sample(held, args.count))

    fresh = {i: rpc.call("ledger", {"ledger_index": i, "transactions": True})["ledger"] for i in sample}
    votes = Counter()
    with open(os.path.join(args.data, "validations.csv"), newline="") as f:
        for r in csv.DictReader(f):
            i = int(r["ledger_index"])
            if i in fresh and r["ledger_hash"] == fresh[i]["ledger_hash"]:
                votes[i] += 1

    bad = 0
    print("ledger_index,hash_match,close_time_match,txn_count_match,canonical_validations,result")
    for i in sample:
        ok, f = compare(ledgers[i], votes[i], fresh[i])
        bad += not ok
        print(f"{i},{f['hash_match']},{f['close_time_match']},{f['txn_count_match']},"
              f"{f['canonical_validations']},{'MATCH' if ok else 'MISMATCH'}")
    print(f"CROSSCHECK {'PASS' if not bad else 'FAIL'}: {args.count - bad}/{args.count} ledgers match")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
