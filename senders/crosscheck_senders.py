#!/usr/bin/env python3
"""Cross-check a senders/ collection against the public RPC (read-only: account_tx, ledger).

Checks, each printed as one OK/MISMATCH line (accounts truncated):
  S  --count random senders (seeded, drawn uniformly from all senders in tx.csv): account_tx
     over the collection's ledger range, paged forward, keeping only transactions SENT by the
     account (tx Account == sender) inside the range. The window is enforced HERE, because the
     public RPC returns transactions outside ledger_index_min/max (acctchurn lesson). The set of
     (hash, ledger, type, result, fee) must equal the local tx.csv rows of that sender.
  D  daily recount: per 24 h slice (same slicing as analyze_senders.py), tx.csv rows must equal
     the sum of the ledger headers' txn_count, and per ledger tx_rows == txn_count.
  L  --ledgers random ledgers (seeded): the public RPC's `ledger` (transactions, not expanded)
     must give the same ledger_hash and the same number of transaction hashes as ledgers.csv.

--negative is the control: every sampled sender's expected set gets one extra hash, every
ledger's expected count gets +1 and every slice's expected total gets +1, so every check must
MISMATCH and the run must exit 1.

Retried public-RPC answers (slowDown, lgrNotFound, internal, transport errors) are counted per
error in the SUMMARY line, never as mismatches; if they persist the run exits 2.
Exit 0 all OK, 1 any mismatch, 2 RPC failure.
"""

import argparse
import os
import random
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "rewardflow"))
from analyze_senders import DAY, read_csv, truncate  # noqa: E402
from collect_rewardflow import RETRY_ERRORS, Rpc, RpcError  # noqa: E402

RETRY = RETRY_ERRORS | {"lgrNotFound", "internal"}
METHODS = ("account_tx", "ledger")


def key_of(hash_, ledger, tt, result, fee):
    return (hash_, int(ledger), tt, result, int(fee))


def local_sets(txs):
    out = defaultdict(set)
    for r in txs:
        k = key_of(r["tx_hash"], r["ledger_index"], r["tx_type"], r["result"], r["fee_drops"])
        out[r["account"]].add(k)
    return out


def rpc_sent(rpc, account, lo, hi, limit=200):
    """Transactions sent by `account` in ledgers lo..hi from paged account_tx; (set, pages, dropped).

    Pages newest first and stops after the first page that reaches below lo: the server may ignore
    the window, so paging forward would walk the account's whole history.
    """
    got, pages, outside = set(), 0, 0
    marker = None
    while True:
        params = {"account": account, "ledger_index_min": lo, "ledger_index_max": hi, "forward": False,
                  "limit": limit}
        if marker:
            params["marker"] = marker
        res = rpc.call("account_tx", params)
        pages += 1
        below = False
        for e in res.get("transactions", []):
            tx = e.get("tx") or e.get("tx_json") or {}
            meta = e.get("meta") or {}
            li = int(tx.get("ledger_index", e.get("ledger_index", 0)))
            if not (lo <= li <= hi):
                outside += 1
                below = below or li < lo
                continue
            if tx.get("Account") != account or not e.get("validated", True):
                continue
            got.add(key_of(tx.get("hash") or e.get("hash"), li, tx.get("TransactionType", ""),
                           meta.get("TransactionResult", ""), tx.get("Fee", 0)))
        marker = res.get("marker")
        if not marker or below:
            return got, pages, outside


def run(data, rpc, count, nledgers, seed, negative=False, out=print):
    txs = read_csv(os.path.join(data, "tx.csv"))
    ledgers = read_csv(os.path.join(data, "ledgers.csv"))
    lo = min(int(r["ledger_index"]) for r in ledgers)
    hi = max(int(r["ledger_index"]) for r in ledgers)
    rng = random.Random(seed)
    res = {"ok": 0, "mismatch": 0}

    def report(ok, line):
        res["ok" if ok else "mismatch"] += 1
        out(f"{'OK      ' if ok else 'MISMATCH'} {line}")

    sets = local_sets(txs)
    senders = rng.sample(sorted(sets), min(count, len(sets)))
    for acct in senders:
        want = set(sets[acct])
        if negative:
            want.add(("NEGATIVE-CONTROL", lo, "Payment", "tesSUCCESS", 0))
        got, pages, outside = rpc_sent(rpc, acct, lo, hi)
        miss_rpc, miss_local = sorted(want - got), sorted(got - want)
        detail = ""
        if miss_rpc or miss_local:
            detail = (f" not_in_rpc={[m[0][:12] for m in miss_rpc[:3]]}"
                      f" not_in_local={[m[0][:12] for m in miss_local[:3]]}")
        report(not miss_rpc and not miss_local,
               f"S {truncate(acct)} local={len(want)} rpc={len(got)} pages={pages} "
               f"outside_window_dropped={outside}{detail}")

    t0 = min(int(r["close_time"]) for r in ledgers)
    t1 = max(int(r["close_time"]) for r in ledgers)
    nslices = max(1, round((t1 - t0) / DAY))
    per_ledger_bad = [r["ledger_index"] for r in ledgers
                      if r.get("tx_rows", r["txn_count"]) != r["txn_count"]]
    rows_by_slice, hdr_by_slice = defaultdict(int), defaultdict(int)
    for r in txs:
        rows_by_slice[min((int(r["close_time"]) - t0) // DAY, nslices - 1)] += 1
    for r in ledgers:
        hdr_by_slice[min((int(r["close_time"]) - t0) // DAY, nslices - 1)] += int(r["txn_count"])
    for s in range(nslices):
        want = hdr_by_slice[s] + (1 if negative else 0)
        report(rows_by_slice[s] == want and not per_ledger_bad,
               f"D slice {s} tx_rows={rows_by_slice[s]} header_txn_count={want} "
               f"ledgers_with_tx_rows_ne_txn_count={len(per_ledger_bad)}")

    for r in sorted(rng.sample(ledgers, min(nledgers, len(ledgers))), key=lambda r: int(r["ledger_index"])):
        led = rpc.call("ledger", {"ledger_index": int(r["ledger_index"]), "transactions": True})["ledger"]
        n = len(led.get("transactions") or [])
        want = int(r["txn_count"]) + (1 if negative else 0)
        report(led.get("ledger_hash") == r["ledger_hash"] and n == want,
               f"L {r['ledger_index']} hash_equal={led.get('ledger_hash') == r['ledger_hash']} "
               f"local_txn_count={want} rpc_txn_count={n}")
    return res, senders


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data", default="data/senders/tx")
    p.add_argument("--count", type=int, default=20)
    p.add_argument("--ledgers", type=int, default=50)
    p.add_argument("--seed", default="1")
    p.add_argument("--negative", action="store_true")
    p.add_argument("--rpc", default="https://rpc.testnet.postfiat.org")
    p.add_argument("--gap", type=float, default=0.5, help="seconds between public RPC requests")
    a = p.parse_args(argv)
    rpc = Rpc(a.rpc, gap=a.gap, retry_errors=RETRY, methods=METHODS)
    try:
        res, _ = run(a.data, rpc, a.count, a.ledgers, a.seed, a.negative)
    except RpcError as e:
        print(f"RPC FAILURE {e}")
        return 2
    print(f"SUMMARY ok={res['ok']} mismatch={res['mismatch']} negative={a.negative} seed={a.seed} "
          f"rpc_calls={rpc.calls} retries={rpc.retried} "
          f"retries_by_error={dict(sorted(rpc.retry_kinds.items()))}")
    return 1 if res["mismatch"] else 0


if __name__ == "__main__":
    sys.exit(main())
