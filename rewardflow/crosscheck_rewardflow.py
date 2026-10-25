#!/usr/bin/env python3
"""Cross-check a collect_rewardflow.py data.jsonl against the public RPC (read-only: tx, account_info).

1. tx refetch: --count random successful outgoing payments (seeded) are fetched again with `tx` and
   every collected field is rebuilt from the answer and compared one by one.
2. Balance reconciliation: for ledger ranges (L1, L2] the account's Balance from `account_info` at L1 and
   L2 is compared with (a) the sum of the collected meta_balance_delta values and (b) the sum derived from
   transaction fields alone (-Fee on own txs, -delivered_amount on own successful payments,
   +delivered_amount on received ones). Ranges whose ends fall outside the server's history are
   reported as IMPOSSIBLE with the RPC error, never as a match.

The public server sometimes gives transient incomplete answers for ledgers it does hold (seen in the
first live run: lgrNotFound and "internal" for account_info inside complete_ledgers, and a `tx` answer
that differed in result/meta; the same queries succeeded seconds later). Such answers are retried
(--tries, exponential backoff) and every retry is printed; only the final answer counts.

Exit 0 = every refetch matches, every possible range reconciles and at least one range was possible.
Usage: crosscheck_rewardflow.py data.jsonl [--count 20] [--seed N] [--rpc URL] [--splits 3]
"""

import argparse
import json
import random
import sys
import time

from collect_rewardflow import RECORD_FIELDS, Rpc, RpcError, native_drops, record


def load(path):
    rng, txs = None, {}
    with open(path) as fh:
        for line in fh:
            e = json.loads(line)
            if e["kind"] == "range":
                rng = e
            elif e["kind"] == "tx":
                txs.setdefault(e["hash"], e)
    return rng, list(txs.values())


def derived_delta(t, account):
    d = 0
    if t["account"] == account and t.get("fee"):
        d -= int(t["fee"])
    if t["type"] == "Payment" and t["result"] == "tesSUCCESS":
        amt = native_drops(t["delivered_amount"])
        if amt is not None:
            if t["account"] == account and t["destination"] != account:
                d -= amt
            elif t["destination"] == account and t["account"] != account:
                d += amt
    return d


TRANSIENT = ("lgrNotFound", "internal", "txnNotFound")


def call_complete(rpc, method, params, complete, tries=4, wait=2.0, out=print):
    """Call until complete(result) is true or a transient error stops recurring; raise on the last try."""
    for i in range(1, tries + 1):
        try:
            r = rpc.call(method, params)
            if complete(r):
                return r
            why = "incomplete answer"
        except RpcError as ex:
            if not any(t in str(ex) for t in TRANSIENT) or i == tries:
                raise
            why = str(ex)
        else:
            if i == tries:
                return r
        out(f"RETRY {method} {json.dumps(params, sort_keys=True)} try {i}/{tries}: {why}; wait {wait:.0f}s")
        rpc.sleep(wait)
        wait *= 2


def tx_complete(r):
    m = r.get("meta") or {}
    return r.get("validated") is True and "TransactionResult" in m and "AffectedNodes" in m


def refetch(rpc, txs, account, count, seed, out=print):
    pays = sorted((t for t in txs if t["direction"] == "out" and t["type"] == "Payment"
                   and t["result"] == "tesSUCCESS"), key=lambda t: t["hash"])
    pick = random.Random(seed).sample(pays, min(count, len(pays)))
    out(f"REFETCH {len(pick)} of {len(pays)} successful outgoing payments (seed {seed}) via tx")
    ok = 0
    for t in pick:
        try:
            r = call_complete(rpc, "tx", {"transaction": t["hash"], "binary": False}, tx_complete, out=out)
            again = record({"tx": r, "meta": r.get("meta") or {}, "validated": r.get("validated")}, account)
            diff = [f for f in RECORD_FIELDS if again.get(f) != t.get(f)]
        except RpcError as ex:
            diff = [f"rpc error {ex}"]
        ok += not diff
        out(f"{'MATCH' if not diff else 'MISMATCH'} {t['hash']} ledger={t['ledger_index']}"
            f" fields={len(RECORD_FIELDS)}" + (f" differ={diff}" if diff else ""))
    out(f"REFETCH SUMMARY {ok}/{len(pick)} match")
    return ok == len(pick) and pick


def balance_at(rpc, account, ledger, out=print):
    r = call_complete(rpc, "account_info", {"account": account, "ledger_index": ledger},
                      lambda r: "account_data" in r, out=out)
    return int(r["account_data"]["Balance"])


def reconcile(rpc, txs, account, ranges, out=print):
    good, done = True, 0
    for lo, hi in ranges:
        inside = [t for t in txs if lo < t["ledger_index"] <= hi]
        meta_sum = sum(t["meta_balance_delta"] or 0 for t in inside)
        der_sum = sum(derived_delta(t, account) for t in inside)
        try:
            b1, b2 = balance_at(rpc, account, lo, out), balance_at(rpc, account, hi, out)
        except RpcError as ex:
            out(f"RANGE ({lo},{hi}] IMPOSSIBLE account_info failed: {ex}"
                " (outside server history; not counted)")
            continue
        delta = b2 - b1
        ok = delta == meta_sum == der_sum
        good &= ok
        done += 1
        out(f"RANGE ({lo},{hi}] {'RECONCILED' if ok else 'MISMATCH'} txs={len(inside)}"
            f" balance {b1} -> {b2} delta={delta} meta_sum={meta_sum} derived_sum={der_sum} (drops)")
    if not done:
        out("RECONCILE no range could be checked: FAIL")
    return good and done > 0


def plan_ranges(rng, txs, splits):
    lo, hi = rng["ledger_min"], rng["ledger_max"]
    led = sorted(t["ledger_index"] for t in txs)
    ranges = [(lo, hi)]
    if led and splits > 1:
        cuts = [lo] + [led[len(led) * i // splits] for i in range(1, splits)] + [hi]
        ranges += [(a, b) for a, b in zip(cuts, cuts[1:], strict=False) if a < b]
    ranges.append((lo - 1, hi))  # starts one ledger before the server's history: must be IMPOSSIBLE
    return ranges


def main(argv=None, rpc=None, sleep=time.sleep):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("data")
    ap.add_argument("--count", type=int, default=20)
    ap.add_argument("--seed", type=int, default=20261025)
    ap.add_argument("--rpc", default="https://rpc.testnet.postfiat.org")
    ap.add_argument("--splits", type=int, default=3)
    a = ap.parse_args(argv)
    rng, txs = load(a.data)
    rpc = rpc or Rpc(a.rpc, gap=1.0, retries=3)
    rpc.sleep = sleep
    account = rng["account"]
    print(f"crosscheck account={account} range {rng['ledger_min']}-{rng['ledger_max']}"
          f" txs={len(txs)} rpc={rpc.url}")
    r1 = refetch(rpc, txs, account, a.count, a.seed)
    r2 = reconcile(rpc, txs, account, plan_ranges(rng, txs, a.splits))
    print(f"RESULT {'PASS' if r1 and r2 else 'FAIL'} refetch={'ok' if r1 else 'fail'}"
          f" reconcile={'ok' if r2 else 'fail'}")
    return 0 if r1 and r2 else 1


if __name__ == "__main__":
    sys.exit(main())
