#!/usr/bin/env python3
"""Exact A->B reconciliation of every account's signing-key posture (Python 3 stdlib only).

Inputs: two state snapshots (state/snapshot.py) and the collect_key.py output directory.

Completeness (exit 2 on any failure, before any replay):
  C1 ledgers.csv holds every ledger A+1..B exactly once, and gaps.csv has no row in that range
  C2 per ledger, key_tx_count equals the key-tx.jsonl lines of that ledger
  C3 no tx hash appears twice
  C4 both snapshots parse, object count = header objects_paged, B is after A
  C5 every AccountRoot index equals its keylet (address decoding self-test), every SignerList
     has a live owner (keylet match), and the optional --count-a/--count-b files (count_key.py)
     say ok for the same ledger

Replay (order: ledger_index, TransactionIndex) applies each tx's AccountRoot and SignerList
metadata to snapshot A's postures. Residuals (exit 1, every one listed):
  R1 per account: the replayed posture (master flag, RegularKey, signer quorum + entries) equals
     snapshot B's, and the replayed account set equals B's
  R2 per class: count(A) + inflow - outflow from the replayed transitions = count(B)
  R3 the replayed SignerList index set equals snapshot B's
  R4 metadata consistency: an AccountRoot created that is already live, a modified/deleted
     AccountRoot that is not live, a PreviousFields RegularKey/flag that differs from the replay
  R5 every account whose posture differs between A and B is touched by a collected tx
--negative drops the first collected tx that has an effect, or (if no tx has one) flips the master
flag of A's first account before replay; either way the exit must then be 1.
Exit codes: 0 PASS, 1 residuals, 2 incomplete input.
"""

import argparse
import csv
import gzip
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from posture_key import (  # noqa: E402
    CLASSES,
    LSF_DISABLE_MASTER,
    account_keylet,
    classify,
    posture,
    signer_summary,
    signerlist_keylet,
)


class Incomplete(Exception):
    pass


def load_snapshot(path):
    """Header, {address: AccountRoot}, {index: SignerList}."""
    accts, sls = {}, {}
    n = 0
    with gzip.open(path, "rt") as fh:
        header = json.loads(fh.readline())
        for line in fh:
            o = json.loads(line)
            n += 1
            if o.get("LedgerEntryType") == "AccountRoot":
                if o["Account"] in accts:
                    raise Incomplete(f"{path}: duplicate AccountRoot {o['Account']}")
                if account_keylet(o["Account"]) != o["index"]:
                    raise Incomplete(f"{path}: AccountRoot index != keylet for {o['Account']}")
                accts[o["Account"]] = o
            elif o.get("LedgerEntryType") == "SignerList":
                sls[o["index"]] = o
    if n != header.get("objects_paged"):
        raise Incomplete(f"{path}: {n} objects, header says {header.get('objects_paged')}")
    return header, accts, sls


def postures(accts, sls):
    """{address: posture} with SignerLists matched to owners by keylet."""
    by_key = {signerlist_keylet(a): a for a in accts}
    owned = {}
    for idx, sl in sls.items():
        if idx not in by_key:
            raise Incomplete(f"SignerList {idx} has no live owner account")
        owned[by_key[idx]] = sl
    return {a: posture(r, owned.get(a)) for a, r in accts.items()}


def load_tx(txdir, a_idx, b_idx):
    """Key-tx lines in A+1..B after completeness checks C1-C3."""
    seen = Counter()
    with open(os.path.join(txdir, "ledgers.csv"), newline="") as fh:
        rows = list(csv.DictReader(fh))
    want = {}
    for r in rows:
        i = int(r["ledger_index"])
        seen[i] += 1
        if a_idx < i <= b_idx:
            want[i] = int(r["key_tx_count"])
    dup = sorted(i for i, c in seen.items() if c > 1 and a_idx < i <= b_idx)
    missing = [i for i in range(a_idx + 1, b_idx + 1) if i not in want]
    if dup or missing:
        raise Incomplete(
            f"C1 ledgers.csv: {len(missing)} missing (first {missing[:5]}), duplicates {dup[:5]}"
        )
    gp = os.path.join(txdir, "gaps.csv")
    if os.path.exists(gp):
        with open(gp, newline="") as fh:
            g = [
                int(r["ledger_index"]) for r in csv.DictReader(fh) if a_idx < int(r["ledger_index"]) <= b_idx
            ]
        if g:
            raise Incomplete(f"C1 gaps.csv has {len(g)} ledgers in range (first {g[:5]})")
    lines, per, hashes = [], Counter(), Counter()
    with open(os.path.join(txdir, "key-tx.jsonl")) as fh:
        for n, raw in enumerate(fh, 1):
            try:
                x = json.loads(raw)
                li = int(x["ledger_index"])
                h = x["tx"]["hash"]
                _ = x["meta"]["AffectedNodes"]
            except (ValueError, KeyError, TypeError) as e:
                raise Incomplete(f"key-tx.jsonl line {n} malformed: {type(e).__name__} {e}") from None
            if a_idx < li <= b_idx:
                per[li] += 1
                hashes[h] += 1
                lines.append(x)
    bad = sorted(i for i in want if want[i] != per.get(i, 0))
    if bad:
        i = bad[0]
        raise Incomplete(
            f"C2 key_tx_count mismatch in {len(bad)} ledgers (first {i}: {want[i]} vs {per.get(i, 0)})"
        )
    d = [h for h, c in hashes.items() if c > 1]
    if d:
        raise Incomplete(f"C3 duplicate tx hash {d[0]} ({len(d)} total)")
    lines.sort(key=lambda x: (int(x["ledger_index"]), int(x["tx_index"])))
    return lines


def check_count(path, header):
    if not path:
        return
    with open(path) as fh:
        c = json.load(fh)
    if c.get("ledger_index") != header["ledger_index"] or c.get("ledger_hash") != header["ledger_hash"]:
        raise Incomplete(f"C5 {path} is for ledger {c.get('ledger_index')}, not {header['ledger_index']}")
    if not c.get("ok"):
        raise Incomplete(f"C5 {path} reports a count mismatch")


def _rank(kind, let):
    # AccountRoot creations/modifications, then SignerList changes, then AccountRoot deletions,
    # so an AccountDelete's SignerList deletion still finds its owner (metadata order is by index)
    if let == "AccountRoot" and kind == "DeletedNode":
        return 2
    return 1 if let == "SignerList" else 0


def ordered_nodes(nodes):
    out = []
    for node in nodes:
        ((kind, v),) = node.items()
        out.append((_rank(kind, v.get("LedgerEntryType")), kind, v))
    return [(k, v) for _, k, v in sorted(out, key=lambda t: t[0])]


def replay(pa, sl_a, lines, owners_extra=()):
    """Apply metadata to A. Returns (postures, signerlist index set, consistency residuals, touched,
    effect line numbers)."""
    cur = {a: dict(p) for a, p in pa.items()}
    sl_idx = set(sl_a)
    by_key = {signerlist_keylet(a): a for a in list(pa) + list(owners_extra)}
    resid, touched, effect = [], set(), []
    for ln, x in enumerate(lines):
        tag = f"{x['ledger_index']}/{x['tx_index']} {x['tx']['hash'][:16]}"
        changed = False
        for kind, v in ordered_nodes(x["meta"]["AffectedNodes"]):
            let = v.get("LedgerEntryType")
            if let == "AccountRoot":
                f = v.get("NewFields") if kind == "CreatedNode" else v.get("FinalFields") or {}
                a = f.get("Account")
                if not a:
                    resid.append(f"R4 {tag}: AccountRoot {kind} without Account")
                    continue
                touched.add(a)
                if kind == "CreatedNode":
                    if a in cur:
                        resid.append(f"R4 {tag}: created AccountRoot {a} is already live")
                    cur[a] = posture(f, None)
                    by_key.setdefault(signerlist_keylet(a), a)
                    changed = True
                    continue
                if a not in cur:
                    resid.append(f"R4 {tag}: {kind} of AccountRoot {a} that is not live")
                    cur[a] = posture(f, None)
                prev = v.get("PreviousFields") or {}
                if "RegularKey" in prev and prev["RegularKey"] != cur[a]["regular_key"]:
                    resid.append(f"R4 {tag}: {a} previous RegularKey differs from replay")
                if "Flags" in prev and (not (int(prev["Flags"]) & LSF_DISABLE_MASTER)) != cur[a]["master"]:
                    resid.append(f"R4 {tag}: {a} previous master flag differs from replay")
                # fields absent from PreviousFields did not change: Flags must equal the replay, and
                # RegularKey must equal it or be newly added (metadata omits added fields there)
                if (
                    "Flags" not in prev
                    and (not (int(f.get("Flags", 0)) & LSF_DISABLE_MASTER)) != cur[a]["master"]
                ):
                    resid.append(f"R4 {tag}: {a} unchanged master flag differs from replay")
                if "RegularKey" not in prev and cur[a]["regular_key"] not in (None, f.get("RegularKey")):
                    resid.append(f"R4 {tag}: {a} unchanged RegularKey differs from replay")
                if kind == "DeletedNode":
                    del cur[a]
                    changed = True
                    continue
                new = posture(f, None)
                new["signers"] = cur[a]["signers"]
                if new != cur[a]:
                    changed = True
                cur[a] = new
            elif let == "SignerList":
                idx = v.get("LedgerIndex")
                a = by_key.get(idx)
                if a is None or a not in cur:
                    resid.append(f"R4 {tag}: SignerList {idx} {kind} has no live owner")
                    continue
                touched.add(a)
                if kind == "DeletedNode":
                    sl_idx.discard(idx)
                    cur[a]["signers"] = None
                else:
                    sl_idx.add(idx)
                    cur[a]["signers"] = signer_summary(
                        v.get("NewFields") if kind == "CreatedNode" else v.get("FinalFields")
                    )
                changed = True
        if changed:
            effect.append(ln)
    return cur, sl_idx, resid, touched, effect


def reconcile(snap_a, snap_b, txdir, negative=False, count_a=None, count_b=None):
    """Full reconciliation result dict. Raises Incomplete."""
    ha, aa, sla = load_snapshot(snap_a)
    hb, ab, slb = load_snapshot(snap_b)
    if hb["ledger_index"] <= ha["ledger_index"]:
        raise Incomplete("C4 snapshot B is not after snapshot A")
    check_count(count_a, ha)
    check_count(count_b, hb)
    pa, pb = postures(aa, sla), postures(ab, slb)
    lines = load_tx(txdir, ha["ledger_index"], hb["ledger_index"])
    neg_note = None
    if negative:
        _, _, _, _, eff = replay(pa, sla, lines, ab)
        if eff:
            x = lines.pop(eff[0])
            tt = x["tx"]["TransactionType"]
            neg_note = f"dropped {x['ledger_index']}/{x['tx_index']} {x['tx']['hash']} ({tt})"
        else:
            # no tx with an effect in the window: tamper snapshot A's posture of its first account instead
            a0 = min(pa)
            pa = dict(pa)
            pa[a0] = dict(pa[a0], master=not pa[a0]["master"])
            neg_note = f"no tx with an effect; flipped the master flag of {a0} in A before replay"
    cur, sl_idx, resid, touched, eff = replay(pa, sla, lines, ab)

    # R1
    for a in sorted(set(cur) | set(pb)):
        if a not in pb:
            resid.append(f"R1 {a}: live after replay, absent in B")
        elif a not in cur:
            resid.append(f"R1 {a}: in B, absent after replay")
        elif cur[a] != pb[a]:
            resid.append(f"R1 {a}: replay {cur[a]} != B {pb[a]}")
    # R2
    ca, cb = Counter(classify(p) for p in pa.values()), Counter(classify(p) for p in pb.values())
    trans = Counter()
    for a in set(pa) | set(cur):
        trans[(classify(pa[a]) if a in pa else "(new)", classify(cur[a]) if a in cur else "(deleted)")] += 1
    per_class = {}
    for c in CLASSES:
        inflow = sum(n for (f, t), n in trans.items() if t == c and f != c)
        outflow = sum(n for (f, t), n in trans.items() if f == c and t != c)
        exp = ca.get(c, 0) + inflow - outflow
        per_class[c] = {"A": ca.get(c, 0), "in": inflow, "out": outflow, "expected_B": exp, "B": cb.get(c, 0)}
        if exp != cb.get(c, 0):
            resid.append(
                f"R2 class {c}: A {ca.get(c, 0)} + in {inflow} - out {outflow} = {exp} != B {cb.get(c, 0)}"
            )
    # R3
    if sl_idx != set(slb):
        resid.append(
            f"R3 SignerList set: replay-only {sorted(sl_idx - set(slb))[:5]} "
            f"B-only {sorted(set(slb) - sl_idx)[:5]}"
        )
    # R5
    changed = sorted(a for a in set(pa) | set(pb) if pa.get(a) != pb.get(a))
    for a in changed:
        if a not in touched:
            resid.append(f"R5 {a}: posture differs A->B but no collected tx touches it")

    return {
        "snap_a": {
            "ledger_index": ha["ledger_index"],
            "ledger_hash": ha["ledger_hash"],
            "accounts": len(pa),
            "signer_lists": len(sla),
        },
        "snap_b": {
            "ledger_index": hb["ledger_index"],
            "ledger_hash": hb["ledger_hash"],
            "accounts": len(pb),
            "signer_lists": len(slb),
        },
        "ledgers": hb["ledger_index"] - ha["ledger_index"],
        "key_tx": len(lines),
        "key_tx_with_effect": len(eff),
        "tx_types": dict(Counter(x["tx"]["TransactionType"] for x in lines)),
        "created": sum(n for (f, _), n in trans.items() if f == "(new)"),
        "deleted": sum(n for (_, t), n in trans.items() if t == "(deleted)"),
        "changed_accounts": changed,
        "transitions": [{"from": f, "to": t, "n": n} for (f, t), n in sorted(trans.items()) if f != t],
        "per_class": per_class,
        "negative": neg_note,
        "residuals": resid,
        "result": "PASS" if not resid else "FAIL",
    }


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--snap-a", required=True)
    p.add_argument("--snap-b", required=True)
    p.add_argument("--tx", required=True, help="collect_key.py --out directory")
    p.add_argument("--count-a", help="count_key.py JSON for snapshot A")
    p.add_argument("--count-b", help="count_key.py JSON for snapshot B")
    p.add_argument("--json", help="write the full result here")
    p.add_argument(
        "--negative",
        action="store_true",
        help="drop the first tx with an effect (else tamper A); must exit 1",
    )
    a = p.parse_args(argv)
    try:
        r = reconcile(a.snap_a, a.snap_b, a.tx, a.negative, a.count_a, a.count_b)
    except (Incomplete, OSError) as e:
        print(f"INCOMPLETE {e}")
        return 2
    if a.json:
        with open(a.json, "w") as fh:
            json.dump(r, fh, indent=1, sort_keys=True)
            fh.write("\n")
    sa, sb = r["snap_a"], r["snap_b"]
    print(
        f"A {sa['ledger_index']} accounts {sa['accounts']} signer_lists {sa['signer_lists']} | "
        f"B {sb['ledger_index']} accounts {sb['accounts']} signer_lists {sb['signer_lists']}"
    )
    print(
        f"ledgers {r['ledgers']} key_tx {r['key_tx']} with_effect {r['key_tx_with_effect']} "
        f"created {r['created']} deleted {r['deleted']} posture_changed {len(r['changed_accounts'])}"
    )
    for c, v in r["per_class"].items():
        if v["A"] or v["B"] or v["in"] or v["out"]:
            print(f"class {c}: A {v['A']} +{v['in']} -{v['out']} = {v['expected_B']} | B {v['B']}")
    if r["negative"]:
        print(f"NEGATIVE {r['negative']}")
    for x in r["residuals"]:
        print(f"RESIDUAL {x}")
    print(f"RESULT {r['result']} residuals {len(r['residuals'])}")
    return 0 if r["result"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
