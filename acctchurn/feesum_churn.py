#!/usr/bin/env python3
"""Sum transaction fees over ledgers A+1..B from a txmix/collect_tx.py run (offline, deterministic).

Inputs: the collector's ledgers.csv (one row per ledger, with txn_count) and tx.csv (one row per
validated transaction, with fee_drops). Every fee in a validated ledger is burned, whatever the
result code, so the sum is the expected total_coins drop between snapshots A and B.

Completeness checks (exit 2 if any fails, nothing is written):
  - every ledger A+1..B is in ledgers.csv exactly once (duplicate rows must be identical)
  - per ledger, the number of distinct tx rows equals that ledger's txn_count
  - a tx_hash that appears twice (collector resume) must have identical rows; it is counted once

Outputs (--out-dir): fees-ledgers.csv (ledger_index, ledger_hash, txn_count, fee_drops) and
fees-tx.csv (ledger_index, tx_index, tx_hash, tx_type, result, fee_drops); no account fields.
Prints one JSON summary line; `fee_sum_drops` is the value for diff_churn.py --fees-sum.

Usage:
  ./feesum_churn.py ledgers.csv tx.csv --from A+1 --to B [--out-dir DIR]
"""

import argparse
import csv
import json
import os
import sys


class FeeError(Exception):
    pass


def feesum(ledger_rows, tx_rows, first, last):
    ledgers = {}
    for r in ledger_rows:
        li = int(r["ledger_index"])
        if not first <= li <= last:
            continue
        row = (r["ledger_hash"], int(r["txn_count"]))
        if li in ledgers and ledgers[li] != row:
            raise FeeError(f"ledger {li}: conflicting duplicate rows")
        ledgers[li] = row
    missing = [i for i in range(first, last + 1) if i not in ledgers]
    if missing:
        raise FeeError(f"{len(missing)} ledgers missing, first {missing[0]}")

    txs, dup = {}, 0
    for r in tx_rows:
        li = int(r["ledger_index"])
        if not first <= li <= last:
            continue
        row = (li, int(r["tx_index"]), r["tx_hash"], r["tx_type"], r["result"], int(r["fee_drops"]))
        if row[2] in txs:
            if txs[row[2]] != row:
                raise FeeError(f"tx {row[2][:16]}: conflicting duplicate rows")
            dup += 1
            continue
        txs[row[2]] = row

    per_ledger = {li: [0, 0] for li in ledgers}
    for li, _, _, _, _, fee in txs.values():
        per_ledger[li][0] += 1
        per_ledger[li][1] += fee
    bad = [li for li in sorted(ledgers) if per_ledger[li][0] != ledgers[li][1]]
    if bad:
        li = bad[0]
        raise FeeError(
            f"{len(bad)} ledgers with tx rows != txn_count, first {li}: "
            f"{per_ledger[li][0]} rows vs txn_count {ledgers[li][1]}"
        )

    ledger_out = [(li, ledgers[li][0], ledgers[li][1], per_ledger[li][1]) for li in sorted(ledgers)]
    tx_out = sorted(txs.values())
    summary = {
        "from": first,
        "to": last,
        "ledgers": len(ledger_out),
        "txs": len(tx_out),
        "duplicate_tx_rows_dropped": dup,
        "fee_sum_drops": sum(r[5] for r in tx_out),
    }
    return summary, ledger_out, tx_out


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("ledgers_csv")
    p.add_argument("tx_csv")
    p.add_argument("--from", dest="first", type=int, required=True, help="A+1")
    p.add_argument("--to", dest="last", type=int, required=True, help="B")
    p.add_argument("--out-dir")
    args = p.parse_args()
    try:
        with open(args.ledgers_csv, newline="") as fl, open(args.tx_csv, newline="") as ft:
            summary, ledger_out, tx_out = feesum(
                csv.DictReader(fl), csv.DictReader(ft), args.first, args.last
            )
    except (FeeError, KeyError, ValueError) as e:
        print(f"FAIL {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    if args.out_dir:
        os.makedirs(args.out_dir, exist_ok=True)
        with open(os.path.join(args.out_dir, "fees-ledgers.csv"), "w", newline="") as fh:
            w = csv.writer(fh, lineterminator="\n")
            w.writerow(["ledger_index", "ledger_hash", "txn_count", "fee_drops"])
            w.writerows(ledger_out)
        with open(os.path.join(args.out_dir, "fees-tx.csv"), "w", newline="") as fh:
            w = csv.writer(fh, lineterminator="\n")
            w.writerow(["ledger_index", "tx_index", "tx_hash", "tx_type", "result", "fee_drops"])
            w.writerows(tx_out)
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
