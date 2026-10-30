#!/usr/bin/env python3
"""Diff two ledger-state snapshots written by state/snapshot.py (offline, deterministic).

Inputs are two snapshot-<ledger>.jsonl.gz files of the SAME chain, A older than B.
Each file is checked before use (exit 2 if not):
  - line 1 is a pft-state-snapshot/1 header
  - the number of object lines equals the header's objects_paged
  - every object has a unique 64-hex `index`

Output (JSON on stdout, or --out FILE), keys sorted so the same inputs give the same bytes:
  - per LedgerEntryType: count in A, count in B, created, deleted, changed (same index,
    different content), unchanged
  - created / deleted AccountRoot lists (address, balance, sequence, OwnerCount)
  - native (PFT drops) conservation, exact to the drop:
        native(S) = sum AccountRoot.Balance + sum Escrow.Amount + sum (PayChannel.Amount - Balance)
    checks  native(A) == total_coins(A), native(B) == total_coins(B), and
            delta_native == delta_total_coins == -burned  (burned = total_coins(A) - total_coins(B))
    and, with --fees-sum N (sum of fee_drops over ledgers A+1..B, from feesum_churn.py),
            burned == fees
  Exit 1 if any conservation check fails, 0 otherwise.

Usage:
  ./diff_churn.py snapA.jsonl.gz snapB.jsonl.gz [--fees-sum N] [--out diff.json]
"""

import argparse
import gzip
import json
import re
import sys
from collections import Counter

HEX64 = re.compile(r"^[0-9A-F]{64}$")


class SnapshotError(Exception):
    pass


def load(path):
    """Return (header, {index: object}) after the integrity checks in the module doc."""
    with gzip.open(path, "rt") as fh:
        first = fh.readline()
        try:
            header = json.loads(first)
        except json.JSONDecodeError as e:
            raise SnapshotError(f"{path}: header is not JSON ({e})") from e
        if header.get("format") != "pft-state-snapshot/1":
            raise SnapshotError(f"{path}: not a pft-state-snapshot/1 file")
        objs = {}
        n = 0
        for line in fh:
            if not line.strip():
                continue
            n += 1
            o = json.loads(line)
            idx = o.get("index", "")
            if not HEX64.match(idx):
                raise SnapshotError(f"{path}: object {n} has no valid index")
            if idx in objs:
                raise SnapshotError(f"{path}: duplicate index {idx}")
            objs[idx] = o
    if n != header.get("objects_paged"):
        raise SnapshotError(f"{path}: {n} objects, header says objects_paged={header.get('objects_paged')}")
    return header, objs


def native_held(objs):
    """Drops held per holder type; IOU amounts (dict) are ignored, only native strings count."""
    acct = escrow = chan = 0
    for o in objs.values():
        t = o.get("LedgerEntryType")
        if t == "AccountRoot":
            acct += int(o["Balance"])
        elif t == "Escrow" and isinstance(o.get("Amount"), str):
            escrow += int(o["Amount"])
        elif t == "PayChannel" and isinstance(o.get("Amount"), str):
            chan += int(o["Amount"]) - int(o.get("Balance", "0"))
    return {"account_root": acct, "escrow": escrow, "paychannel": chan, "total": acct + escrow + chan}


def account_row(o):
    return {
        "address": o["Account"],
        "balance_drops": int(o["Balance"]),
        "sequence": o.get("Sequence"),
        "owner_count": o.get("OwnerCount", 0),
        "previous_txn_id": o.get("PreviousTxnID"),
        "previous_txn_lgr_seq": o.get("PreviousTxnLgrSeq"),
    }


def diff(ha, a, hb, b, fees_sum=None):
    if hb["ledger_index"] <= ha["ledger_index"]:
        raise SnapshotError("snapshot B must be a later ledger than A")
    types = sorted(
        {o.get("LedgerEntryType", "?") for o in a.values()}
        | {o.get("LedgerEntryType", "?") for o in b.values()}
    )
    per = {t: Counter() for t in types}
    created_acct, deleted_acct, changed_acct = [], [], 0
    for idx, o in a.items():
        t = o.get("LedgerEntryType", "?")
        per[t]["in_a"] += 1
        if idx not in b:
            per[t]["deleted"] += 1
            if t == "AccountRoot":
                deleted_acct.append(account_row(o))
        elif b[idx] != o:
            per[t]["changed"] += 1
            changed_acct += t == "AccountRoot"
        else:
            per[t]["unchanged"] += 1
    for idx, o in b.items():
        t = o.get("LedgerEntryType", "?")
        per[t]["in_b"] += 1
        if idx not in a:
            per[t]["created"] += 1
            if t == "AccountRoot":
                created_acct.append(account_row(o))

    na, nb = native_held(a), native_held(b)
    burned = ha["total_coins"] - hb["total_coins"]
    checks = {
        "native_a_eq_total_coins_a": na["total"] == ha["total_coins"],
        "native_b_eq_total_coins_b": nb["total"] == hb["total_coins"],
        "delta_native_eq_minus_burned": nb["total"] - na["total"] == -burned,
    }
    if fees_sum is not None:
        checks["burned_eq_fee_sum"] = burned == fees_sum
    return {
        "a": {
            k: ha[k]
            for k in ("ledger_index", "ledger_hash", "close_time_iso", "total_coins", "objects_paged")
        },
        "b": {
            k: hb[k]
            for k in ("ledger_index", "ledger_hash", "close_time_iso", "total_coins", "objects_paged")
        },
        "by_type": {
            t: {k: per[t][k] for k in ("in_a", "in_b", "created", "deleted", "changed", "unchanged")}
            for t in types
        },
        "accounts": {
            "created": sorted(created_acct, key=lambda r: r["address"]),
            "deleted": sorted(deleted_acct, key=lambda r: r["address"]),
            "changed": changed_acct,
        },
        "native": {"a": na, "b": nb, "burned_drops": burned, "fee_sum_drops": fees_sum},
        "checks": checks,
        "ok": all(checks.values()),
    }


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("snap_a")
    p.add_argument("snap_b")
    p.add_argument("--fees-sum", type=int, help="sum of fee_drops over ledgers A+1..B")
    p.add_argument("--out")
    args = p.parse_args(argv)
    try:
        ha, a = load(args.snap_a)
        hb, b = load(args.snap_b)
        result = diff(ha, a, hb, b, args.fees_sum)
    except SnapshotError as e:
        print(f"ERROR {e}", file=sys.stderr)
        return 2
    text = json.dumps(result, sort_keys=True, indent=1) + "\n"
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text)
    for name, ok in sorted(result["checks"].items()):
        print(f"{'PASS' if ok else 'FAIL'} {name}", file=sys.stderr)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
