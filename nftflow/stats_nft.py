#!/usr/bin/env python3
"""NFT lifecycle statistics for one reconciled window (Python 3 stdlib; matplotlib only for --chart).

    stats_nft.py --reconcile R.json --snap-a A.jsonl.gz --snap-b B.jsonl.gz --tx DIR
                 [--json OUT] [--chart PNG]

R.json is reconcile_nft.py --json output for the same snapshots and tx directory. Stats are
only computed from a PASSING reconciliation (every check passed, no residuals, not --negative);
anything else exits 2, so every number below rests on an exactly reconciled event list.

Sections (all offline, deterministic, same inputs -> byte-identical JSON):
  hourly     mints / burns / transfers / offer creates per hour since snapshot A
  issuers    per issuer: live at A and B, mints, burns, MintedNFTokens/BurnedNFTokens deltas
  taxon/flag NFTokenID flag combinations, distinct taxons (unscrambled from the ID), top taxons,
             for NFTs live at B plus NFTs minted in the window
  fees       TransferFee distribution (basis points of 1/100000, shown as %)
  lifetime   mint -> burn seconds for NFTs minted AND burned in the window; burns of NFTs
             minted before A are counted separately (left-censored)
  offers     offers created in the window by fate (accepted, cancelled, removed with a burn,
             other, still open at B), sell/buy split, create -> accept seconds
  pages      NFTokenPage fill (tokens per page, max 32) at A and B, owners with >1 page

Mint field check (exit 1 on any mismatch, all listed): for every successful NFTokenMint the
minted NFTokenID's flags, transfer fee, issuer and unscrambled taxon must equal the tx's
Flags (low 16 bits), TransferFee, Issuer-or-Account and NFTokenTaxon.
"""

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

import reconcile_nft as rec

RIPPLE_EPOCH = 946684800
PAGE_MAX = 32
FLAG_NAMES = [
    (0x0001, "burnable"),
    (0x0002, "only_xrp"),
    (0x0004, "trustline"),
    (0x0008, "transferable"),
    (0x0010, "mutable"),
]


class StatsError(Exception):
    pass


def taxon_of(nft_id):
    """Unscramble the taxon stored in an NFTokenID (XLS-20: cipher = taxon ^ (384160001 * seq + 2459))."""
    seq = rec.serial_of(nft_id)
    cipher = int(nft_id[48:56], 16)
    return cipher ^ ((384160001 * seq + 2459) & 0xFFFFFFFF)


def flag_label(flags):
    names = [n for bit, n in FLAG_NAMES if flags & bit]
    other = flags & ~sum(b for b, _ in FLAG_NAMES)
    if other:
        names.append(f"0x{other:04X}")
    return "+".join(names) or "none"


def fee_pct(fee):
    return f"{fee / 1000:.3f}%"


def quantiles(xs):
    if not xs:
        return None
    s = sorted(xs)

    def q(p):
        return s[min(len(s) - 1, int(p * (len(s) - 1) + 0.5))]

    return {
        "n": len(s),
        "min": s[0],
        "p50": q(0.5),
        "p90": q(0.9),
        "max": s[-1],
        "mean": round(sum(s) / len(s), 1),
    }


def page_fill(pages):
    fills = [n for _, n in pages]
    owners = Counter(o for o, _ in pages)
    buckets = Counter()
    for n in fills:
        buckets[f"{(max(n, 1) - 1) // 8 * 8 + 1}-{(max(n, 1) - 1) // 8 * 8 + 8}"] += 1
    return {
        "pages": len(fills),
        "tokens": sum(fills),
        "owners": len(owners),
        "owners_multi_page": sum(1 for c in owners.values() if c > 1),
        "mean_fill": round(sum(fills) / len(fills), 2) if fills else 0,
        "fill_ratio": round(sum(fills) / (PAGE_MAX * len(fills)), 4) if fills else 0,
        "full_pages": sum(1 for n in fills if n == PAGE_MAX),
        "buckets": dict(sorted(buckets.items(), key=lambda kv: int(kv[0].split("-")[0]))),
    }


def mint_field_check(recs):
    """Compare each successful NFTokenMint's tx fields with the NFTokenID it created."""
    checked, problems = 0, []
    for r in recs:
        tx = r["tx"]
        if tx["TransactionType"] != "NFTokenMint" or r["meta"].get("TransactionResult") != "tesSUCCESS":
            continue
        effects, _, _ = rec.tx_effects(r["meta"])
        minted = [nid for kind, nid, *_ in effects if kind == "mint"]
        if len(minted) != 1:
            problems.append(f"{tx['hash']}: {len(minted)} minted IDs in metadata")
            continue
        nid = minted[0]
        issuer = rec._account_hex(tx.get("Issuer") or tx["Account"])
        want = {
            "flags": int(tx.get("Flags", 0)) & 0xFFFF,
            "fee": int(tx.get("TransferFee", 0)),
            "issuer": issuer,
            "taxon": int(tx["NFTokenTaxon"]),
        }
        got = {
            "flags": rec.flags_of(nid),
            "fee": rec.fee_of(nid),
            "issuer": rec.issuer_of(nid),
            "taxon": taxon_of(nid),
        }
        for k in want:
            if want[k] != got[k]:
                problems.append(f"{tx['hash']}: {k} tx {want[k]} != NFTokenID {got[k]}")
        checked += 1
    return checked, problems


def compute(res, objs_a, objs_b, recs, start_epoch):
    own_a, off_a, pages_a, _ = rec.snapshot_state(objs_a)
    own_b, off_b, pages_b, _ = rec.snapshot_state(objs_b)
    ev = res["events"]

    def hour(e):
        return (e["close_time"] + RIPPLE_EPOCH - start_epoch) // 3600

    end_hour = (res["_end_epoch"] - start_epoch) // 3600
    outside = [e["tx_hash"] for e in ev if not 0 <= hour(e) <= end_hour]
    if outside:
        raise StatsError(f"{len(outside)} events outside snapshot A close .. B close (first {outside[0]})")
    hourly = defaultdict(Counter)
    for e in ev:
        if e["kind"] in ("mint", "burn", "transfer", "offer_create"):
            hourly[hour(e)][e["kind"]] += 1
    hourly_rows = [
        {"hour": h, **{k: hourly[h][k] for k in ("mint", "burn", "transfer", "offer_create")}}
        for h in range(end_hour + 1)
    ]

    # issuers
    la = Counter(rec.issuer_of(n) for n in own_a)
    lb = Counter(rec.issuer_of(n) for n in own_b)
    mints = Counter(rec.issuer_of(e["nft_id"]) for e in ev if e["kind"] == "mint")
    burns = Counter(rec.issuer_of(e["nft_id"]) for e in ev if e["kind"] == "burn")
    counters = {c["issuer"]: c for c in res["counters"]}
    issuers = []
    for iss in sorted(set(la) | set(lb) | set(mints) | set(burns)):
        c = counters.get(iss, {})
        issuers.append(
            {
                "issuer": rec.account_id_to_address(iss),
                "live_a": la[iss],
                "live_b": lb[iss],
                "mints": mints[iss],
                "burns": burns[iss],
                "minted_delta": c.get("minted_delta", 0),
                "burned_delta": c.get("burned_delta", 0),
            }
        )
    issuers.sort(key=lambda r: (-(r["mints"] + r["burns"]), -r["live_b"], r["issuer"]))

    # taxon / flags / fees over NFTs live at B plus minted in the window
    population = set(own_b) | {e["nft_id"] for e in ev if e["kind"] == "mint"}
    flags = Counter(flag_label(rec.flags_of(n)) for n in population)
    taxons = Counter((rec.issuer_of(n), taxon_of(n)) for n in population)
    fees = Counter(rec.fee_of(n) for n in population)
    uri = sum(1 for e in ev if e["kind"] == "mint" and e["uri"])

    # lifetime
    mint_t = {e["nft_id"]: e["close_time"] for e in ev if e["kind"] == "mint"}
    life, pre_window = [], 0
    for e in ev:
        if e["kind"] == "burn":
            if e["nft_id"] in mint_t:
                life.append(e["close_time"] - mint_t[e["nft_id"]])
            else:
                pre_window += 1
    minted_live_b = sum(1 for n in mint_t if n in own_b)

    # offers
    created = {e["offer"]: e for e in ev if e["kind"] == "offer_create"}
    deleted = {e["offer"]: e for e in ev if e["kind"] == "offer_delete"}
    fate_names = {
        "NFTokenAcceptOffer": "accepted",
        "NFTokenCancelOffer": "cancelled",
        "NFTokenBurn": "removed_with_burn",
    }
    fates = Counter()
    accept_secs = []
    for idx, c in created.items():
        d = deleted.get(idx)
        if d is None:
            fates["open_at_b" if idx in off_b else "missing"] += 1
            continue
        f = fate_names.get(d["tx_type"], "other")
        fates[f] += 1
        if f == "accepted":
            accept_secs.append(d["close_time"] - c["close_time"])
    pre_fates = Counter(
        fate_names.get(d["tx_type"], "other") for idx, d in deleted.items() if idx not in created
    )

    return {
        "window": {
            "ledger_a": res["ledger_a"],
            "ledger_b": res["ledger_b"],
            "close_a": res["close_a"],
            "close_b": res["close_b"],
            "nft_txs": res["nft_txs"],
            "live_a": res["live_a"],
            "live_b": res["live_b"],
            "mints": res["mints"],
            "burns": res["burns"],
            "transfers": res["transfers"],
            "page_moves": res["moves"],
            "mints_with_uri": uri,
        },
        "hourly": hourly_rows,
        "issuers": issuers,
        "flags": dict(sorted(flags.items(), key=lambda kv: (-kv[1], kv[0]))),
        "taxons": {
            "population": len(population),
            "distinct_issuer_taxon": len(taxons),
            "top": [
                {"issuer": rec.account_id_to_address(i), "taxon": t, "nfts": n}
                for (i, t), n in sorted(taxons.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
            ],
        },
        "transfer_fees": [{"fee": f, "pct": fee_pct(f), "nfts": n} for f, n in sorted(fees.items())],
        "lifetime": {
            "minted_and_burned": quantiles(life),
            "burned_minted_before_a": pre_window,
            "minted_still_live_at_b": minted_live_b,
        },
        "offers": {
            "at_a": len(off_a),
            "at_b": len(off_b),
            "created": len(created),
            "created_sell": sum(1 for c in created.values() if c["sell"]),
            "created_buy": sum(1 for c in created.values() if not c["sell"]),
            "fates": dict(sorted(fates.items())),
            "pre_window_offers_deleted": dict(sorted(pre_fates.items())),
            "create_to_accept_s": quantiles(accept_secs),
        },
        "pages": {"a": page_fill(pages_a), "b": page_fill(pages_b)},
    }


def chart(stats, path):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import MaxNLocator

    rows = stats["hourly"]
    w = stats["window"]
    hs = [r["hour"] for r in rows]
    fig, ax = plt.subplots(figsize=(8, 3.2), facecolor="#fcfcfb")
    ax.set_facecolor("#fcfcfb")
    bw = 0.4
    ax.bar([h - bw / 2 for h in hs], [r["mint"] for r in rows], width=bw, color="#2a78d6", label="mints")
    ax.bar([h + bw / 2 for h in hs], [r["burn"] for r in rows], width=bw, color="#e34948", label="burns")
    ax.set_xlabel(f"hour since snapshot A ({stats['window']['close_a']})", fontsize=8)
    ax.set_ylabel("NFTs per hour", fontsize=8)
    ax.set_title(
        f"NFT mints and burns per hour, ledgers {w['ledger_a']} -> {w['ledger_b']}",
        fontsize=9,
    )
    ax.set_xlim(-0.6, len(hs) - 0.4)
    ax.xaxis.set_major_locator(MaxNLocator(integer=True))
    ax.yaxis.set_major_locator(MaxNLocator(integer=True))
    ax.tick_params(labelsize=7)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.legend(fontsize=7, frameon=False)
    fig.tight_layout()
    fig.savefig(path, dpi=120, metadata={"Software": None})
    plt.close(fig)


def iso_epoch(s):
    return int(datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp())


def load_inputs(a):
    with open(a.reconcile) as fh:
        res = json.load(fh)
    if res.get("negative_dropped") or res["residuals"] or not all(c["pass"] for c in res["checks"]):
        raise StatsError("reconciliation did not pass (or is a --negative run); refusing to compute stats")
    ha, oa = rec.load_snapshot(a.snap_a)
    hb, ob = rec.load_snapshot(a.snap_b)
    if (ha["ledger_index"], hb["ledger_index"]) != (res["ledger_a"], res["ledger_b"]):
        raise StatsError(
            f"snapshots {ha['ledger_index']}->{hb['ledger_index']}"
            f" != reconcile {res['ledger_a']}->{res['ledger_b']}"
        )
    recs = rec.read_tx_dir(a.tx, ha["ledger_index"], hb["ledger_index"])
    if len(recs) != res["nft_txs"]:
        raise StatsError(f"tx dir has {len(recs)} NFT txs, reconcile used {res['nft_txs']}")
    res["_end_epoch"] = iso_epoch(hb["close_time_iso"])
    return res, oa, ob, recs, iso_epoch(ha["close_time_iso"])


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--reconcile", required=True, help="reconcile_nft.py --json output (passing run)")
    p.add_argument("--snap-a", required=True)
    p.add_argument("--snap-b", required=True)
    p.add_argument("--tx", required=True, help="collect_nft.py output directory")
    p.add_argument("--json", default=None, help="write the statistics here")
    p.add_argument("--chart", default=None, help="hourly mint/burn PNG")
    a = p.parse_args(argv)
    try:
        res, oa, ob, recs, start = load_inputs(a)
    except (StatsError, rec.SnapshotError, rec.IncompleteError, OSError, KeyError, ValueError) as e:
        print(f"INCOMPLETE: {e}")
        return 2
    try:
        st = compute(res, oa, ob, recs, start)
    except StatsError as e:
        print(f"INCOMPLETE: {e}")
        return 2
    checked, problems = mint_field_check(recs)
    st["mint_field_check"] = {"checked": checked, "mismatches": problems}
    w = st["window"]
    print(
        f"window {w['ledger_a']} -> {w['ledger_b']}: {w['nft_txs']} NFT txs, "
        f"live {w['live_a']} -> {w['live_b']}, "
        f"mints {w['mints']} ({w['mints_with_uri']} with URI), burns {w['burns']}, transfers {w['transfers']}"
    )
    print(
        f"issuers {len(st['issuers'])}, distinct issuer/taxon {st['taxons']['distinct_issuer_taxon']}, "
        f"flags {st['flags']}"
    )
    print(f"lifetime {st['lifetime']}")
    print(f"offers {st['offers']}")
    for k in ("a", "b"):
        pg = st["pages"][k]
        print(
            f"pages {k.upper()}: {pg['pages']} pages, {pg['tokens']} tokens, "
            f"mean fill {pg['mean_fill']}/{PAGE_MAX}"
        )
    print(f"mint field check: {checked} mints, {len(problems)} mismatches")
    for s in problems[:50]:
        print(f"MISMATCH {s}")
    if a.json:
        with open(a.json, "w") as fh:
            json.dump(st, fh, indent=1, sort_keys=True)
    if a.chart:
        chart(st, a.chart)
        print(f"chart {os.path.basename(a.chart)}")
    print("RESULT: PASS" if not problems else "RESULT: FAIL")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
