#!/usr/bin/env python3
"""Cross-check a passing reconcile_nft.py result against the public RPC (read-only).

    crosscheck_nft.py RECONCILE.json SNAP_A SNAP_B [--count 20] [--owners 3] [--seed 1]
                      [--negative] [--rpc URL]

Checks, each printed as one OK/MISMATCH line (accounts and IDs truncated):
  X1 --count seeded mints and --count seeded burns from the reconciliation's events: `tx` of the
     tx hash is validated, in the same ledger, of the same TransactionType, tesSUCCESS, and names
     the same NFTokenID (meta.nftoken_id, which the server computes; the reconciliation derived
     the ID independently from NFTokenPage diffs). Same for every transfer (or --count of them).
  X2 the largest owner at B plus --owners - 1 seeded others: the full `account_nfts` list at ledger B
     (all marker pages) is exactly snapshot B's NFTokenID set for that owner.
  X3 every sampled burn: the burned ID is absent from its last owner's `account_nfts` at B.
  X4 every issuer with a mint or burn in the window: `account_info` at A and at B gives the
     snapshot's MintedNFTokens / BurnedNFTokens (absent = 0).
  X5 --count seeded NFTokenOffers live at B: `ledger_entry` at B returns the same object;
     every offer deleted in the window answers entryNotFound at B (up to --count).

--negative is the control: every expected value is altered (last hex digit of each ID flipped,
one ID removed from each owner set, counters +1, offer owner changed), so every check must
MISMATCH and the run must exit 1.

lgrNotFound / internal / slowDown answers are retried with backoff and counted per error in the
SUMMARY line (retries_by_error); they are never counted as mismatches. If they persist, exit 2.

Exit 0 all OK, 1 any mismatch, 2 RPC failure or a reconciliation that did not pass.
"""

import argparse
import importlib.util
import json
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def _load_module(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, "..", rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_rec = _load_module("_nftflow_reconcile_x", "nftflow/reconcile_nft.py")
_fund = _load_module("_nftflow_funders_churn", "acctchurn/funders_churn.py")
RpcError = _fund.RpcError
churn_rpc = _fund.churn_rpc
rpc_stats = _fund.rpc_stats
to_address = _rec.account_id_to_address
READ_ONLY_METHODS = frozenset({"tx", "account_nfts", "account_info", "ledger_entry"})


def short(s):
    return s[:8] + "…"


def flip(nid):
    """Same ID with its last hex digit changed (negative control)."""
    return nid[:-1] + ("0" if nid[-1] != "0" else "1")


def account_nft_ids(rpc, address, ledger, limit=400):
    ids, marker = set(), None
    while True:
        params = {"account": address, "ledger_index": ledger, "limit": limit}
        if marker:
            params["marker"] = marker
        try:
            res = rpc.call("account_nfts", params)
        except RpcError as e:
            if "actNotFound" in str(e):
                return ids
            raise
        ids.update(t["NFTokenID"] for t in res.get("account_nfts", []))
        marker = res.get("marker")
        if not marker:
            return ids


def counters(rpc, address, ledger):
    """(MintedNFTokens, BurnedNFTokens) at `ledger`, or None for actNotFound."""
    try:
        res = rpc.call("account_info", {"account": address, "ledger_index": ledger})
    except RpcError as e:
        if "actNotFound" in str(e):
            return None
        raise
    d = res["account_data"]
    return int(d.get("MintedNFTokens", 0)), int(d.get("BurnedNFTokens", 0))


def ledger_entry(rpc, index, ledger):
    try:
        return rpc.call("ledger_entry", {"index": index, "ledger_index": ledger})["node"]
    except RpcError as e:
        if "entryNotFound" in str(e):
            return None
        raise


def tx_nft_id(tx):
    meta = tx.get("meta") or tx.get("meta_json") or {}
    body = tx.get("tx_json") or tx
    return meta.get("nftoken_id") or body.get("NFTokenID")


def tx_type(tx):
    return (tx.get("tx_json") or tx).get("TransactionType")


def tx_result(tx):
    return (tx.get("meta") or tx.get("meta_json") or {}).get("TransactionResult")


def run(res, objs_a, objs_b, rpc, count, owners, seed, negative=False, out=print):
    la, lb = res["ledger_a"], res["ledger_b"]
    own_b, off_b, _, acc_b = _rec.snapshot_state(objs_b)
    _, _, _, acc_a = _rec.snapshot_state(objs_a)
    rnd = random.Random(seed)
    bad = checked = 0

    def report(ok, what):
        nonlocal bad, checked
        checked += 1
        bad += not ok
        out(f"{'OK' if ok else 'MISMATCH'} {what}")

    ev = res["events"]
    mints = [e for e in ev if e["kind"] == "mint"]
    burns = [e for e in ev if e["kind"] == "burn"]
    transfers = [e for e in ev if e["kind"] == "transfer"]

    def sample(pool, n):
        return rnd.sample(pool, min(n, len(pool)))

    s_mints, s_burns, s_tr = sample(mints, count), sample(burns, count), sample(transfers, count)

    # X1 tx lookups
    for e in s_mints + s_burns + s_tr:
        exp = flip(e["nft_id"]) if negative else e["nft_id"]
        tx = rpc.call("tx", {"transaction": e["tx_hash"]})
        got = tx_nft_id(tx)
        ok = (
            tx.get("validated") is True
            and tx.get("ledger_index") == e["ledger_index"]
            and tx_type(tx) == e["tx_type"]
            and tx_result(tx) == "tesSUCCESS"
            and got == exp
        )
        report(
            ok,
            f"X1 {e['kind']} tx {short(e['tx_hash'])} ledger={e['ledger_index']} {e['tx_type']}"
            f" id …{exp[-8:]} got …{(got or '-')[-8:]} result={tx_result(tx)}",
        )

    # X2 owner sets at B
    by_owner = {}
    for nid, o in own_b.items():
        by_owner.setdefault(o, set()).add(nid)
    burned_from = sorted({e["from"] for e in s_burns})
    pool = sorted(by_owner)
    # the largest owner is always checked (exercises account_nfts paging), plus seeded others
    big = max(pool, key=lambda o: (len(by_owner[o]), o)) if pool and owners > 0 else None
    chosen = ([big] if big else []) + sample([o for o in pool if o != big], owners - 1)
    cache = {}
    for o in chosen:
        addr = to_address(o)
        exp = set(by_owner[o])
        if negative:
            exp.discard(min(exp))
        got = cache[o] = account_nft_ids(rpc, addr, lb)
        report(
            got == exp,
            f"X2 owner {short(addr)} account_nfts @B expected={len(exp)} got={len(got)}"
            f" missing={len(exp - got)} unexpected={len(got - exp)}",
        )

    # X3 burned IDs absent at B
    for o in burned_from:
        if o not in cache:
            cache[o] = account_nft_ids(rpc, to_address(o), lb)
    for e in s_burns:
        nid = e["nft_id"]
        present = nid in cache[e["from"]]
        report(present is negative, f"X3 burned …{nid[-8:]} absent from {short(to_address(e['from']))} @B")

    # X4 issuer counters at A and B
    issuers = sorted({r["issuer"] for r in res["counters"] if r["mints"] or r["burns"]})
    for iss in issuers:
        addr = to_address(iss)
        for led, accs, tag in ((la, acc_a, "A"), (lb, acc_b, "B")):
            o = accs.get(addr)
            exp = None if o is None else (int(o.get("MintedNFTokens", 0)), int(o.get("BurnedNFTokens", 0)))
            if negative and exp is not None:
                exp = (exp[0] + 1, exp[1])
            got = counters(rpc, addr, led)
            report(got == exp, f"X4 issuer {short(addr)} @{tag} Minted/Burned expected={exp} got={got}")

    # X5 offers
    for idx in sample(sorted(off_b), count):
        o = off_b[idx]
        node = ledger_entry(rpc, idx, lb)
        exp_owner = "rrrrrrrrrrrrrrrrrrrrrhoLvTp" if negative else o.get("Owner")
        ok = (
            node is not None
            and node.get("NFTokenID") == o.get("NFTokenID")
            and node.get("Owner") == exp_owner
            and node.get("Amount") == o.get("Amount")
        )
        report(ok, f"X5 offer {short(idx)} live @B")
    gone = sorted({e["offer"] for e in ev if e["kind"] == "offer_delete"} - set(off_b))
    for idx in sample(gone, count):
        node = ledger_entry(rpc, idx, lb)
        report((node is None) is not negative, f"X5 deleted offer {short(idx)} entryNotFound @B")

    st = rpc_stats(rpc)
    out(
        f"SUMMARY checked={checked} mismatches={bad} ledgers={la}->{lb}"
        f" mints={len(s_mints)}/{len(mints)} burns={len(s_burns)}/{len(burns)}"
        f" transfers={len(s_tr)}/{len(transfers)} owners={len(chosen)}/{len(pool)}"
        f" issuers={len(issuers)} offers_live={min(count, len(off_b))}/{len(off_b)}"
        f" offers_deleted={min(count, len(gone))}/{len(gone)} negative={negative}"
        f" rpc_calls={st['calls']} retries={st['retries']}"
        f" retries_by_error={json.dumps(st['retries_by_error'], sort_keys=True)}"
    )
    return 1 if bad else 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("reconcile_json")
    p.add_argument("snap_a")
    p.add_argument("snap_b")
    p.add_argument("--count", type=int, default=20)
    p.add_argument("--owners", type=int, default=3)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--negative", action="store_true")
    p.add_argument("--rpc", default="https://rpc.testnet.postfiat.org")
    p.add_argument("--gap", type=float, default=0.5)
    args = p.parse_args(argv)
    with open(args.reconcile_json) as fh:
        res = json.load(fh)
    if not all(c["pass"] for c in res["checks"]) or res.get("negative_dropped"):
        print("ERROR reconciliation did not pass (or is a --negative run); nothing to cross-check")
        return 2
    try:
        ha, objs_a = _rec.load_snapshot(args.snap_a)
        hb, objs_b = _rec.load_snapshot(args.snap_b)
    except (_rec.SnapshotError, OSError) as e:
        print(f"ERROR {e}")
        return 2
    if (ha["ledger_index"], hb["ledger_index"]) != (res["ledger_a"], res["ledger_b"]):
        print("ERROR snapshots are not the reconciliation's A and B")
        return 2
    try:
        return run(
            res,
            objs_a,
            objs_b,
            churn_rpc(args.rpc, gap=args.gap, methods=READ_ONLY_METHODS),
            args.count,
            args.owners,
            args.seed,
            args.negative,
        )
    except RpcError as e:
        print(f"ERROR {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
