#!/usr/bin/env python3
"""Exact NFT supply reconciliation between two ledger-state snapshots (Python 3 stdlib only).

    reconcile_nft.py --snap-a A.jsonl.gz --snap-b B.jsonl.gz --tx DIR [--json OUT] [--negative]

Inputs: two state/snapshot.py snapshots (ledgers A < B) and a collect_nft.py directory
(nft-tx.jsonl + ledgers.csv + gaps.csv) covering ledgers A+1..B.

Completeness (exit 2 if any fails, nothing is reconciled on incomplete data):
  every ledger A+1..B has exactly one ledgers.csv row, gaps.csv is empty, each ledger's
  nft_tx_count equals its nft-tx.jsonl lines, no tx hash appears twice, both snapshots pass
  acctchurn/diff_churn.load (header format, object count, unique index).

Replay: every tx's metadata is turned into per-NFTokenID effects by diffing the NFTokens arrays
of the NFTokenPage nodes it touched (owner = first 20 bytes of the page index):
  mint      absent before the tx, present after       transfer  owner changed
  burn      present before the tx, absent after       move      same owner, other page
Those effects are applied in (ledger, TransactionIndex) order to snapshot A's NFTokenID->owner
map; a mint of a live ID, a burn or transfer of an ID that is not live with that owner is a
residual. Offers are replayed the same way (CreatedNode / DeletedNode NFTokenOffer).

Checks (exit 1 if any fails, every residual is listed):
  R1 replayed A == B NFTokenID->owner map (missing, unexpected, wrong owner)
  R2 per issuer: live(B) == live(A) + mints - burns, and overall
  R3 per issuer AccountRoot: MintedNFTokens(B) - MintedNFTokens(A) == mints,
     BurnedNFTokens(B) - BurnedNFTokens(A) == burns (absent account / field = 0)
  R4 replayed A offers == B offers (by index)
  R5 every mint's NFTokenID sequence field is new for its issuer (no reused ID)

--negative drops the first successful mint before the replay; the run must then exit 1.
"""

import argparse
import csv
import importlib.util
import json
import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))


def _load_module(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, "..", rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_churn = _load_module("_nftflow_diff_churn", "acctchurn/diff_churn.py")
SnapshotError = _churn.SnapshotError
load_snapshot = _churn.load
account_id_to_address = _load_module("_nftflow_state_analyze", "state/analyze.py").account_id_to_address


class IncompleteError(Exception):
    pass


def issuer_of(nft_id):
    return nft_id[8:48]


def flags_of(nft_id):
    return int(nft_id[0:4], 16)


def fee_of(nft_id):
    return int(nft_id[4:8], 16)


def serial_of(nft_id):
    return int(nft_id[56:64], 16)


def page_tokens(fields):
    return {t["NFToken"]["NFTokenID"]: t["NFToken"] for t in (fields or {}).get("NFTokens", [])}


def snapshot_state(objs):
    """(NFTokenID -> owner AccountID hex, offers {index: obj}, pages [(owner, n)], accounts {id: obj})."""
    owner = {}
    offers = {}
    pages = []
    accounts = {}
    for idx, o in objs.items():
        t = o.get("LedgerEntryType")
        if t == "NFTokenPage":
            toks = page_tokens(o)
            for nid in toks:
                if nid in owner:
                    raise SnapshotError(f"NFTokenID {nid} on two pages")
                owner[nid] = idx[:40]
            pages.append((idx[:40], len(toks)))
        elif t == "NFTokenOffer":
            offers[idx] = o
        elif t == "AccountRoot":
            accounts[o["Account"]] = o
    return owner, offers, pages, accounts


def tx_effects(meta):
    """Per-tx NFT effects: list of (kind, nft_id, owner_before, owner_after, token) and offer deltas."""
    before, after = {}, {}
    created_offers, deleted_offers = {}, {}
    for node in meta.get("AffectedNodes", []):
        ((kind, n),) = node.items()
        let = n.get("LedgerEntryType")
        idx = n.get("LedgerIndex", "")
        if let == "NFTokenPage":
            own = idx[:40]
            if kind == "CreatedNode":
                for nid, tok in page_tokens(n.get("NewFields")).items():
                    after[nid] = (own, tok)
            elif kind == "ModifiedNode":
                prev = n.get("PreviousFields") or {}
                if "NFTokens" not in prev:
                    continue
                for nid, tok in page_tokens(prev).items():
                    before[nid] = (own, tok)
                for nid, tok in page_tokens(n.get("FinalFields")).items():
                    after[nid] = (own, tok)
            elif kind == "DeletedNode":
                prev = n.get("PreviousFields") or {}
                src = prev if "NFTokens" in prev else n.get("FinalFields")
                for nid, tok in page_tokens(src).items():
                    before[nid] = (own, tok)
        elif let == "NFTokenOffer":
            if kind == "CreatedNode":
                created_offers[idx] = dict(n.get("NewFields") or {}, index=idx, LedgerEntryType=let)
            elif kind == "DeletedNode":
                deleted_offers[idx] = dict(n.get("FinalFields") or {}, index=idx)
    effects = []
    for nid in sorted(set(before) | set(after)):
        ob = before.get(nid, (None, None))[0]
        oa, tok = after.get(nid, (None, before.get(nid, (None, None))[1]))
        if ob is None:
            kind = "mint"
        elif oa is None:
            kind = "burn"
        elif ob != oa:
            kind = "transfer"
        else:
            kind = "move"
        effects.append((kind, nid, ob, oa, tok))
    return effects, created_offers, deleted_offers


def read_tx_dir(txdir, a, b):
    """Completeness checks; returns the nft-tx.jsonl records for ledgers A+1..B in replay order."""
    problems = []
    seen = Counter()
    want = {}
    with open(os.path.join(txdir, "ledgers.csv"), newline="") as fh:
        for row in csv.DictReader(fh):
            li = int(row["ledger_index"])
            seen[li] += 1
            want[li] = int(row["nft_tx_count"])
    missing = [li for li in range(a + 1, b + 1) if seen[li] == 0]
    dup = sorted(li for li in range(a + 1, b + 1) if seen[li] > 1)
    if missing:
        problems.append(f"{len(missing)} ledgers missing in A+1..B (first {missing[:5]})")
    if dup:
        problems.append(f"{len(dup)} ledgers listed twice (first {dup[:5]})")
    gaps = os.path.join(txdir, "gaps.csv")
    if os.path.exists(gaps):
        with open(gaps) as fh:
            n_gaps = max(0, sum(1 for _ in fh) - 1)
        if n_gaps:
            problems.append(f"gaps.csv lists {n_gaps} ledgers")
    recs = []
    got = Counter()
    hashes = Counter()
    with open(os.path.join(txdir, "nft-tx.jsonl")) as fh:
        for line in fh:
            r = json.loads(line)
            li = r["ledger_index"]
            if a < li <= b:
                recs.append(r)
                got[li] += 1
                hashes[r["tx"]["hash"]] += 1
    bad = [li for li in range(a + 1, b + 1) if seen[li] and got[li] != want[li]]
    if bad:
        problems.append(f"{len(bad)} ledgers where nft_tx_count != jsonl lines (first {bad[:5]})")
    twice = [h for h, c in hashes.items() if c > 1]
    if twice:
        problems.append(f"{len(twice)} tx hashes appear twice (first {twice[:3]})")
    if problems:
        raise IncompleteError("; ".join(problems))
    recs.sort(key=lambda r: (r["ledger_index"], r["tx_index"]))
    return recs


def reconcile(ha, objs_a, hb, objs_b, recs, negative=False):
    a, b = ha["ledger_index"], hb["ledger_index"]
    own_a, off_a, pages_a, acc_a = snapshot_state(objs_a)
    own_b, off_b, pages_b, acc_b = snapshot_state(objs_b)
    live = dict(own_a)
    offers = dict(off_a)
    residuals = []
    events = []
    mints = Counter()
    burns = Counter()
    dropped = None
    for r in recs:
        effects, c_off, d_off = tx_effects(r["meta"])
        for kind, nid, ob, oa, tok in effects:
            if negative and dropped is None and kind == "mint":
                dropped = nid
                continue
            ev = {
                "kind": kind,
                "nft_id": nid,
                "from": ob,
                "to": oa,
                "ledger_index": r["ledger_index"],
                "close_time": r["close_time"],
                "tx_type": r["tx"]["TransactionType"],
                "tx_hash": r["tx"]["hash"],
                "uri": bool(tok and tok.get("URI")),
            }
            events.append(ev)
            if kind == "mint":
                if nid in live:
                    residuals.append(f"R1 mint of live {nid} in {r['tx']['hash']}")
                live[nid] = oa
                mints[issuer_of(nid)] += 1
            elif kind == "burn":
                if live.get(nid) != ob:
                    residuals.append(f"R1 burn of {nid} not live with owner {ob} in {r['tx']['hash']}")
                live.pop(nid, None)
                burns[issuer_of(nid)] += 1
            elif kind == "transfer":
                if live.get(nid) != ob:
                    residuals.append(f"R1 transfer of {nid} not live with owner {ob} in {r['tx']['hash']}")
                live[nid] = oa
        for idx in d_off:
            if offers.pop(idx, None) is None:
                residuals.append(f"R4 deleted offer {idx} was not live in {r['tx']['hash']}")
        for idx, o in c_off.items():
            if idx in offers:
                residuals.append(f"R4 created offer {idx} already live in {r['tx']['hash']}")
            offers[idx] = o
        for idx, o in c_off.items():
            events.append(
                {
                    "kind": "offer_create",
                    "offer": idx,
                    "nft_id": o.get("NFTokenID"),
                    "sell": bool(int(o.get("Flags", 0)) & 1),
                    "ledger_index": r["ledger_index"],
                    "close_time": r["close_time"],
                    "tx_type": r["tx"]["TransactionType"],
                    "tx_hash": r["tx"]["hash"],
                }
            )
        for idx, o in d_off.items():
            events.append(
                {
                    "kind": "offer_delete",
                    "offer": idx,
                    "nft_id": o.get("NFTokenID"),
                    "ledger_index": r["ledger_index"],
                    "close_time": r["close_time"],
                    "tx_type": r["tx"]["TransactionType"],
                    "tx_hash": r["tx"]["hash"],
                }
            )

    checks = []

    # R1 owner map
    r1 = []
    for nid in sorted(set(live) | set(own_b)):
        x, y = live.get(nid), own_b.get(nid)
        if x != y:
            r1.append(f"R1 {nid}: replayed owner {x}, snapshot B owner {y}")
    residuals += r1
    checks.append(
        (
            "R1 replayed A == B NFTokenID->owner map",
            not any(s.startswith("R1") for s in residuals),
            f"{len(own_b)} live at B, {len(r1)} differences",
        )
    )

    # R2 per issuer supply
    la = Counter(issuer_of(n) for n in own_a)
    lb = Counter(issuer_of(n) for n in own_b)
    r2 = []
    for iss in sorted(set(la) | set(lb) | set(mints) | set(burns)):
        if la[iss] + mints[iss] - burns[iss] != lb[iss]:
            r2.append(f"R2 issuer {iss}: {la[iss]} + {mints[iss]} - {burns[iss]} != {lb[iss]}")
    tot_ok = len(own_a) + sum(mints.values()) - sum(burns.values()) == len(own_b)
    if not tot_ok:
        r2.append(f"R2 total: {len(own_a)} + {sum(mints.values())} - {sum(burns.values())} != {len(own_b)}")
    residuals += r2
    checks.append(
        (
            "R2 per-issuer and total supply",
            not r2,
            f"{len(own_a)} + {sum(mints.values())} - {sum(burns.values())} = {len(own_b)}"
            f" over {len(set(la) | set(lb))} issuers",
        )
    )

    # R3 AccountRoot counters
    r3 = []
    issuers = set(mints) | set(burns)
    for o in list(acc_a.values()) + list(acc_b.values()):
        if "MintedNFTokens" in o or "BurnedNFTokens" in o:
            issuers.add(_account_hex(o["Account"]))
    by_hex_a = {_account_hex(k): v for k, v in acc_a.items()}
    by_hex_b = {_account_hex(k): v for k, v in acc_b.items()}
    counter_rows = []
    for iss in sorted(issuers):
        oa_, ob_ = by_hex_a.get(iss, {}), by_hex_b.get(iss, {})
        dm = int(ob_.get("MintedNFTokens", 0)) - int(oa_.get("MintedNFTokens", 0))
        db = int(ob_.get("BurnedNFTokens", 0)) - int(oa_.get("BurnedNFTokens", 0))
        counter_rows.append(
            {"issuer": iss, "minted_delta": dm, "mints": mints[iss], "burned_delta": db, "burns": burns[iss]}
        )
        if dm != mints[iss]:
            r3.append(f"R3 issuer {iss}: MintedNFTokens delta {dm} != mints {mints[iss]}")
        if db != burns[iss]:
            r3.append(f"R3 issuer {iss}: BurnedNFTokens delta {db} != burns {burns[iss]}")
    residuals += r3
    checks.append(
        (
            "R3 AccountRoot MintedNFTokens/BurnedNFTokens deltas",
            not r3,
            f"{len(counter_rows)} issuer accounts",
        )
    )

    # R4 offers
    r4 = [
        f"R4 offer {i}: replayed {'live' if i in offers else 'absent'}, "
        f"B {'live' if i in off_b else 'absent'}"
        for i in sorted(set(offers) ^ set(off_b))
    ]
    residuals += r4
    checks.append(
        (
            "R4 replayed A offers == B offers",
            not any(s.startswith("R4") for s in residuals),
            f"{len(off_a)} at A, {len(off_b)} at B",
        )
    )

    # R5 no reused IDs: a minted ID is unique among A + all mints
    minted_ids = [e["nft_id"] for e in events if e["kind"] == "mint"]
    reused = [n for n, c in Counter(minted_ids).items() if c > 1] + [n for n in minted_ids if n in own_a]
    r5 = [f"R5 NFTokenID {n} minted more than once" for n in sorted(set(reused))]
    residuals += r5
    checks.append(("R5 every minted NFTokenID is new", not r5, f"{len(minted_ids)} mints"))

    return {
        "ledger_a": a,
        "ledger_b": b,
        "close_a": ha.get("close_time_iso"),
        "close_b": hb.get("close_time_iso"),
        "live_a": len(own_a),
        "live_b": len(own_b),
        "mints": sum(mints.values()),
        "burns": sum(burns.values()),
        "transfers": sum(1 for e in events if e["kind"] == "transfer"),
        "moves": sum(1 for e in events if e["kind"] == "move"),
        "offers_a": len(off_a),
        "offers_b": len(off_b),
        "nft_txs": len(recs),
        "pages_a": len(pages_a),
        "pages_b": len(pages_b),
        "checks": [{"name": n, "pass": ok, "detail": d} for n, ok, d in checks],
        "residuals": residuals,
        "negative_dropped": dropped,
        "counters": counter_rows,
        "events": events,
    }


_HEX_CACHE = {}


def _account_hex(address):
    """Classic address -> AccountID hex (upper), via base58 (ripple alphabet) decode."""
    h = _HEX_CACHE.get(address)
    if h is None:
        alpha = "rpshnaf39wBUDNEGHJKLM4PQRST7VWXYZ2bcdeCg65jkm8oFqi1tuvAxyz"
        n = 0
        for ch in address:
            n = n * 58 + alpha.index(ch)
        raw = n.to_bytes(25, "big")
        h = raw[1:21].hex().upper()
        if account_id_to_address(h) != address:
            raise ValueError(f"bad address {address}")
        _HEX_CACHE[address] = h
    return h


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--snap-a", required=True)
    p.add_argument("--snap-b", required=True)
    p.add_argument("--tx", required=True, help="collect_nft.py output directory")
    p.add_argument("--json", default=None, help="write the result (with events) here")
    p.add_argument("--negative", action="store_true", help="drop the first mint; must exit 1")
    a = p.parse_args(argv)
    try:
        ha, oa = load_snapshot(a.snap_a)
        hb, ob = load_snapshot(a.snap_b)
        if hb["ledger_index"] <= ha["ledger_index"]:
            raise SnapshotError("snapshot B must be later than A")
        recs = read_tx_dir(a.tx, ha["ledger_index"], hb["ledger_index"])
    except (SnapshotError, IncompleteError, OSError) as e:
        print(f"INCOMPLETE: {e}")
        return 2
    res = reconcile(ha, oa, hb, ob, recs, negative=a.negative)
    print(
        f"window ledgers {res['ledger_a']} ({res['close_a']}) -> {res['ledger_b']} ({res['close_b']}), "
        f"{res['nft_txs']} NFT txs, completeness OK (every ledger once, 0 gaps, counts match, 0 dup hashes)"
    )
    print(
        f"live NFTs {res['live_a']} -> {res['live_b']}: mints {res['mints']}, burns {res['burns']}, "
        f"transfers {res['transfers']}, page moves {res['moves']}; "
        f"offers {res['offers_a']} -> {res['offers_b']}"
    )
    if a.negative:
        print(f"NEGATIVE CONTROL: dropped mint {res['negative_dropped']}")
    for c in res["checks"]:
        print(f"{'PASS' if c['pass'] else 'FAIL'} {c['name']}: {c['detail']}")
    for s in res["residuals"][:50]:
        print(f"RESIDUAL {s}")
    if len(res["residuals"]) > 50:
        print(f"... {len(res['residuals']) - 50} more residuals")
    print(f"residuals: {len(res['residuals'])}")
    if a.json:
        with open(a.json, "w") as fh:
            json.dump(res, fh, indent=1, sort_keys=True)
    ok = all(c["pass"] for c in res["checks"])
    print("RESULT: PASS" if ok else "RESULT: FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
