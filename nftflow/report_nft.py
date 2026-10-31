#!/usr/bin/env python3
"""Write nftflow/REPORT.md from reconcile_nft.py --json and stats_nft.py --json output.

Offline and deterministic: the same inputs give byte-identical output. Every number in the
report comes from the two JSON files (or the SUMMARY lines of the crosscheck logs); nothing is
typed by hand. Accounts, NFTokenIDs and hashes are truncated to 8 characters.

Refuses (exit 2) a reconciliation that did not pass, a --negative one, or stats from another window.

Usage:
  ./report_nft.py RECONCILE.json STATS.json --out nftflow/REPORT.md [--top 10]
      [--chart hourly.png] [--crosscheck LOG ...] [--tests "N passed"] [--mutants "MUTANTS ..."]
"""

import argparse
import json
import os
import sys


def short(a):
    return (a or "?")[:8] + "…"


def hms(s):
    if s is None:
        return "-"
    h, r = divmod(int(s), 3600)
    m, s = divmod(r, 60)
    return f"{h}h{m:02d}m{s:02d}s"


def summary_lines(path):
    with open(path, encoding="utf-8") as f:
        return [ln.rstrip("\n") for ln in f if ln.startswith(("SUMMARY", "RESULT"))]


def build(rec, st, top=10, chart=None, crosscheck=(), tests=None, mutants=None):
    w = st["window"]
    out = ["# nftflow: NFT lifecycle and supply reconciliation", ""]
    out += [
        f"- Snapshot A: ledger {w['ledger_a']} ({w['close_a']})",
        f"- Snapshot B: ledger {w['ledger_b']} ({w['close_b']})",
        f"- Window: {w['ledger_b'] - w['ledger_a']} ledgers (A+1..B), "
        f"{w['nft_txs']} transactions touching NFTs",
        "",
        "## Reconciliation",
        "",
        "| Check | Result | Detail |",
        "|---|---|---|",
    ]
    out += [f"| {c['name']} | {'PASS' if c['pass'] else 'FAIL'} | {c['detail']} |" for c in rec["checks"]]
    out += [
        "",
        f"Residuals: {len(rec['residuals'])}. "
        f"Live NFTs {w['live_a']} + mints {w['mints']} - burns {w['burns']} "
        f"= {w['live_a'] + w['mints'] - w['burns']} (snapshot B: {w['live_b']}).",
        f"Transfers (owner changed): {w['transfers']}. Page moves (same owner, other page; split or merge): "
        f"{w['page_moves']}. Mints with a URI: {w['mints_with_uri']} of {w['mints']}.",
        f"Mint field check (flags, fee, issuer, taxon of each minted ID vs its mint tx): "
        f"{st['mint_field_check']['checked']} mints, {len(st['mint_field_check']['mismatches'])} mismatches.",
        "",
        "## Hourly activity",
        "",
    ]
    if chart:
        out += [f"![mints and burns per hour]({chart})", ""]
    out += ["| hour since A | mints | burns | transfers | offers created |", "|---:|---:|---:|---:|---:|"]
    out += [
        f"| {h['hour']} | {h['mint']} | {h['burn']} | {h['transfer']} | {h['offer_create']} |"
        for h in st["hourly"]
    ]
    busy = max(st["hourly"], key=lambda h: (h["mint"] + h["burn"], -h["hour"]))
    out += ["", f"Busiest hour: {busy['hour']} ({busy['mint']} mints, {busy['burn']} burns).", ""]

    iss = st["issuers"]
    active = [i for i in iss if i["mints"] or i["burns"]]
    out += [
        "## Issuers",
        "",
        f"{len(iss)} issuers have NFTs live at A or B or activity in the window; "
        f"{len(active)} minted or burned.",
        "",
        f"| Issuer (top {top} by mints + burns) | live A | live B | mints | burns "
        "| MintedNFTokens Δ | BurnedNFTokens Δ |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for i in sorted(active, key=lambda i: (-(i["mints"] + i["burns"]), i["issuer"]))[:top]:
        out.append(
            f"| `{short(i['issuer'])}` | {i['live_a']} | {i['live_b']} | {i['mints']} | {i['burns']} "
            f"| {i['minted_delta']} | {i['burned_delta']} |"
        )
    t = st["taxons"]
    out += [
        "",
        "## Flags, taxons, transfer fees",
        "",
        f"Population: {t['population']} NFTs (live at B plus minted in the window).",
        "",
        "| flags | NFTs |",
        "|---|---:|",
    ]
    out += [f"| {k} | {v} |" for k, v in sorted(st["flags"].items(), key=lambda kv: (-kv[1], kv[0]))]
    out += [
        "",
        f"Distinct (issuer, taxon) pairs: {t['distinct_issuer_taxon']}.",
        "",
        f"| Issuer | taxon | NFTs (top {len(t['top'])}) |",
        "|---|---:|---:|",
    ]
    out += [f"| `{short(x['issuer'])}` | {x['taxon']} | {x['nfts']} |" for x in t["top"]]
    out += ["", "| TransferFee | NFTs | share |", "|---:|---:|---:|"]
    out += [f"| {f['fee']} | {f['nfts']} | {f['pct']} |" for f in st["transfer_fees"]]

    lt = st["lifetime"]
    mb = lt["minted_and_burned"]
    out += [
        "",
        "## NFT lifetime (mint -> burn)",
        "",
        f"- Minted and burned inside the window: {mb['n']}; lifetime min {hms(mb['min'])}, "
        f"median {hms(mb['p50'])}, p90 {hms(mb['p90'])}, max {hms(mb['max'])}, mean {hms(mb['mean'])}",
        "- Burned in the window but minted before A (left-censored, no lifetime): "
        f"{lt['burned_minted_before_a']}",
        f"- Minted in the window and still live at B: {lt['minted_still_live_at_b']}",
    ]
    o = st["offers"]
    out += [
        "",
        "## Offers",
        "",
        f"- NFTokenOffer objects: {o['at_a']} at A, {o['at_b']} at B",
        f"- Created in the window: {o['created']} (sell {o['created_sell']}, buy {o['created_buy']})",
        f"- Fates of offers created in the window: {json.dumps(o['fates'], sort_keys=True)}",
        f"- Offers that existed at A and were deleted in the window: "
        f"{json.dumps(o['pre_window_offers_deleted'], sort_keys=True)}",
        f"- Create -> accept time: {o['create_to_accept_s'] if o['create_to_accept_s'] is not None else '-'}",
        "",
        "## NFTokenPage efficiency (max 32 tokens per page)",
        "",
        "| | pages | tokens | mean fill | fill ratio | full pages | owners | owners with >1 page |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for k in ("a", "b"):
        p = st["pages"][k]
        out.append(
            f"| {k.upper()} | {p['pages']} | {p['tokens']} | {p['mean_fill']} | {p['fill_ratio']} "
            f"| {p['full_pages']} | {p['owners']} | {p['owners_multi_page']} |"
        )
    buckets = sorted(st["pages"]["a"]["buckets"], key=lambda b: int(b.split("-")[0]))
    out += ["", "| tokens per page | " + " | ".join(buckets) + " |", "|---|" + "---:|" * len(buckets)]
    for k in ("a", "b"):
        out.append(
            f"| {k.upper()} | " + " | ".join(str(st["pages"][k]["buckets"][b]) for b in buckets) + " |"
        )
    if crosscheck or tests or mutants:
        out += ["", "## Verification runs", ""]
        for name, lines in crosscheck:
            out += [f"- `{name}`: " + "; ".join(f"`{ln}`" for ln in lines)]
        if tests:
            out.append(f"- tests: `{tests}`")
        if mutants:
            out.append(f"- mutants: `{mutants}`")
    return "\n".join(out) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("reconcile")
    ap.add_argument("stats")
    ap.add_argument("--out", required=True)
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--chart")
    ap.add_argument("--crosscheck", action="append", default=[])
    ap.add_argument("--tests")
    ap.add_argument("--mutants")
    a = ap.parse_args(argv)
    with open(a.reconcile, encoding="utf-8") as f:
        rec = json.load(f)
    with open(a.stats, encoding="utf-8") as f:
        st = json.load(f)
    failed = not all(c["pass"] for c in rec["checks"]) or bool(rec["residuals"])
    negative = rec.get("negative_dropped") is not None
    if failed or negative:
        print("refusing: reconciliation did not pass (or is a --negative run)")
        return 2
    w = st["window"]
    if (w["ledger_a"], w["ledger_b"]) != (rec["ledger_a"], rec["ledger_b"]):
        print("refusing: stats and reconciliation cover different windows")
        return 2
    xc = [(os.path.basename(p), summary_lines(p)) for p in a.crosscheck]
    text = build(rec, st, a.top, a.chart, xc, a.tests, a.mutants)
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"wrote {a.out}: {len(text.splitlines())} lines")
    return 0


if __name__ == "__main__":
    sys.exit(main())
