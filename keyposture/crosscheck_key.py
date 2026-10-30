#!/usr/bin/env python3
"""Cross-check a passing reconcile_key.py result against the public RPC (read-only).

    crosscheck_key.py RECONCILE.json SNAP_A SNAP_B [--count 20] [--seed 1] [--extra ADDR ...]
                      [--negative] [--rpc URL]

Account set: every account that is not master_only at A or at B, every account whose posture
changed A->B, every --extra address, then seeded random others until there are >= --count.
Checks, one OK/MISMATCH line each (addresses truncated):
  K1 `account_info` (signer_lists: true) at ledger B: the posture built from the server's
     Flags, RegularKey and SignerList equals snapshot B's (actNotFound = absent from B)
  K2 the same at ledger A against snapshot A
  K3 up to --count txs with an effect: `tx` is validated, in the same ledger, same type, tesSUCCESS
--negative alters every expected posture (master flag flipped) and tx ledger (+1), so every check
must MISMATCH and the run must exit 1.

lgrNotFound / internal / slowDown answers are retried with backoff and counted per error in the
SUMMARY line; they are never counted as mismatches. If they persist, exit 2.
Exit 0 all OK, 1 any mismatch, 2 RPC failure or a reconciliation that did not pass.
"""

import argparse
import importlib.util
import json
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import reconcile_key as R  # noqa: E402
from posture_key import classify, posture  # noqa: E402


def _load_module(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, "..", rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_fund = _load_module("_keyposture_funders_churn", "acctchurn/funders_churn.py")
RpcError = _fund.RpcError
churn_rpc = _fund.churn_rpc
rpc_stats = _fund.rpc_stats
READ_ONLY_METHODS = frozenset({"tx", "account_info"})


def short(s):
    return s[:8] + "…"


def server_posture(rpc, address, ledger):
    """Posture at `ledger` from account_info (signer_lists), or None for actNotFound."""
    try:
        res = rpc.call("account_info", {"account": address, "ledger_index": ledger, "signer_lists": True})
    except RpcError as e:
        if "actNotFound" in str(e):
            return None
        raise
    d = res["account_data"]
    lists = res.get("signer_lists", d.get("signer_lists")) or []
    if len(lists) > 1:
        raise RpcError(f"account_info: {len(lists)} signer lists for {address}")
    return posture(d, lists[0] if lists else None)


def pick_accounts(pa, pb, changed, extra, count, seed):
    keep = {a for a, p in pa.items() if classify(p) != "master_only"}
    keep |= {a for a, p in pb.items() if classify(p) != "master_only"}
    keep |= set(changed) | set(extra)
    others = sorted((set(pa) | set(pb)) - keep)
    rnd = random.Random(seed)
    need = max(0, count - len(keep))
    return sorted(keep), sorted(rnd.sample(others, min(need, len(others))))


def describe(p):
    return "absent" if p is None else classify(p)


def run(res, pa, pb, rpc, count, seed, extra=(), negative=False, out=print):
    la, lb = res["snap_a"]["ledger_index"], res["snap_b"]["ledger_index"]
    must, sampled = pick_accounts(pa, pb, res["changed_accounts"], extra, count, seed)
    out(f"accounts: {len(must)} required (non-master_only, changed, extra) + {len(sampled)} sampled")
    bad = checked = 0

    def report(ok, what):
        nonlocal bad, checked
        checked += 1
        bad += not ok
        out(f"{'OK' if ok else 'MISMATCH'} {what}")

    def expect(p):
        if p is None or not negative:
            return p
        return dict(p, master=not p["master"])

    for a in must + sampled:
        for tag, led, snap in (("K1", lb, pb), ("K2", la, pa)):
            want = expect(snap.get(a))
            if negative and want is None:
                want = {"master": False, "regular_key": None, "signers": None}
            got = server_posture(rpc, a, led)
            report(got == want, f"{tag} {short(a)} @{led} server {describe(got)} snapshot {describe(want)}")
    rnd = random.Random(seed)
    txs = res.get("effect_txs", [])
    for t in rnd.sample(txs, min(count, len(txs))):
        r = rpc.call("tx", {"transaction": t["hash"]})
        meta = r.get("meta") or r.get("meta_json") or {}
        body = r.get("tx_json") or r
        led = t["ledger_index"] + (1 if negative else 0)
        ok = (
            r.get("validated") is True
            and int(r.get("ledger_index", -1)) == led
            and body.get("TransactionType") == t["type"]
            and meta.get("TransactionResult") == "tesSUCCESS"
        )
        report(
            ok,
            f"K3 {short(t['hash'])} {t['type']} ledger {r.get('ledger_index')} expect {led} "
            f"{meta.get('TransactionResult')}",
        )
    st = rpc_stats(rpc)
    out(
        f"SUMMARY checked={checked} mismatches={bad} accounts={len(must) + len(sampled)} "
        f"required={len(must)} "
        f"effect_txs={len(txs)} negative={negative} rpc_calls={st['calls']} retries={st['retries']} "
        f"retries_by_error={st['retries_by_error']}"
    )
    return 1 if bad else 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("reconcile")
    p.add_argument("snap_a")
    p.add_argument("snap_b")
    p.add_argument("--count", type=int, default=20)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--extra", nargs="*", default=[])
    p.add_argument("--negative", action="store_true")
    p.add_argument("--rpc", default="https://rpc.testnet.postfiat.org")
    p.add_argument("--gap", type=float, default=0.5)
    a = p.parse_args(argv)
    with open(a.reconcile) as fh:
        res = json.load(fh)
    if res.get("result") != "PASS" or res.get("negative"):
        print("reconciliation did not pass (or is a --negative run): refusing")
        return 2
    try:
        ha, aa, sla = R.load_snapshot(a.snap_a)
        hb, ab, slb = R.load_snapshot(a.snap_b)
    except (R.Incomplete, OSError) as e:
        print(f"bad snapshot: {e}")
        return 2
    if (ha["ledger_index"], hb["ledger_index"]) != (
        res["snap_a"]["ledger_index"],
        res["snap_b"]["ledger_index"],
    ):
        print("snapshots do not match the reconciliation")
        return 2
    try:
        return run(
            res,
            R.postures(aa, sla),
            R.postures(ab, slb),
            churn_rpc(a.rpc, gap=a.gap, methods=READ_ONLY_METHODS),
            a.count,
            a.seed,
            a.extra,
            a.negative,
        )
    except RpcError as e:
        print(f"RPC FAILURE {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
