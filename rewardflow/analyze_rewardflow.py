#!/usr/bin/env python3
"""Analyze a collect_rewardflow.py data.jsonl: the account's PFT payment outflows, written to REPORT.md.

Counts only validated outgoing Payments with result tesSUCCESS and a native (drops) delivered_amount,
and uses delivered_amount (never Amount) as the paid value. Non-tesSUCCESS outgoing payments are
reported as failed; partial payments (tfPartialPayment flag or delivered < Amount) are counted.
Addresses in the report are truncated to their first 6 characters.

Usage: analyze_rewardflow.py data.jsonl --out-dir rewardflow/report
"""

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime

DROPS = 1_000_000
TF_PARTIAL = 0x00020000
SIZE_BUCKETS = [(0, 1), (1, 5), (5, 10), (10, 100), (100, 1000), (1000, 10000), (10000, None)]


def load(path):
    rng, done, txs, seen = None, None, [], set()
    with open(path) as fh:
        for line in fh:
            if not line.endswith("\n"):
                break  # torn last line: ignore, the collector repairs it on resume
            e = json.loads(line)
            if e["kind"] == "range":
                rng = e
            elif e["kind"] == "done":
                done = e
            elif e["kind"] == "tx" and e["hash"] not in seen:
                seen.add(e["hash"])
                txs.append(e)
    return rng, done, txs


def native(v):
    return int(v) if isinstance(v, str) and v.isdigit() else None


def classify(txs, account):
    paid, failed, partial, other = [], [], 0, Counter()
    for t in txs:
        if t["type"] != "Payment" or t["account"] != account:
            other[f"{t['direction']}:{t['type']}"] += 1
            continue
        if t["result"] != "tesSUCCESS":
            failed.append(t)
            continue
        d = native(t["delivered_amount"])
        if d is None:
            other["out:Payment(non-native)"] += 1
            continue
        amt = native(t["amount"])
        if t.get("flags", 0) & TF_PARTIAL or (amt is not None and d < amt):
            partial += 1
        paid.append(dict(t, paid=d))
    return paid, failed, partial, other


def percentile(vals, p):
    if not vals:
        return None
    s = sorted(vals)
    k = (len(s) - 1) * p / 100
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def gini(vals):
    s = sorted(vals)
    n = len(s)
    if n == 0 or sum(s) == 0:
        return 0.0
    cum = sum((i + 1) * v for i, v in enumerate(s))
    return (2 * cum) / (n * sum(s)) - (n + 1) / n


def bucket_label(lo, hi):
    return f">= {lo:g}" if hi is None else f"{lo:g} - <{hi:g}"


def bucket(sizes):
    out = {}
    for lo, hi in SIZE_BUCKETS:
        out[bucket_label(lo, hi)] = sum(1 for s in sizes if s >= lo and (hi is None or s < hi))
    return out


def ts(t):
    return datetime.strptime(t, "%Y-%m-%dT%H:%M:%SZ")


def analyze(txs, account):
    paid, failed, partial, other = classify(txs, account)
    paid.sort(key=lambda t: (t["ledger_index"], t["close_time"]))
    per_day, per_hour = defaultdict(int), defaultdict(int)
    by_rcpt = Counter()
    cnt_rcpt = Counter()
    memo = Counter()
    for t in paid:
        per_day[t["close_time"][:10]] += t["paid"]
        per_hour[t["close_time"][:13] + ":00Z"] += t["paid"]
        by_rcpt[t["destination"]] += t["paid"]
        cnt_rcpt[t["destination"]] += 1
        ms = t.get("memos") or [{"type": None, "format": None, "kind": None}]
        for m in ms:
            memo[(m["type"], m["format"], m["kind"])] += 1
    sizes = [t["paid"] / DROPS for t in paid]
    pairs = zip(paid, paid[1:], strict=False)
    gaps = [(ts(b["close_time"]) - ts(a["close_time"])).total_seconds() for a, b in pairs]
    total = sum(by_rcpt.values())
    shares = [v / total for v in by_rcpt.values()] if total else []
    top10 = sum(sorted(by_rcpt.values(), reverse=True)[:10])
    return {
        "paid": paid, "failed": failed, "partial": partial, "other": other,
        "count": len(paid), "total_pft": total / DROPS, "unique": len(by_rcpt),
        "per_day": dict(sorted(per_day.items())), "per_hour": dict(sorted(per_hour.items())),
        "sizes": sizes, "size_pct": {p: percentile(sizes, p) for p in (50, 90, 99)},
        "size_max": max(sizes) if sizes else None, "size_hist": bucket(sizes),
        "top10_share": top10 / total if total else None,
        "hhi": sum(s * s for s in shares), "gini": gini(list(by_rcpt.values())),
        "repeat_rate": (sum(1 for c in cnt_rcpt.values() if c > 1) / len(cnt_rcpt)) if cnt_rcpt else None,
        "by_rcpt": by_rcpt, "cnt_rcpt": cnt_rcpt,
        "gaps": gaps, "gap_pct": {p: percentile(gaps, p) for p in (50, 90, 99)},
        "gap_max": max(gaps) if gaps else None, "memo": memo,
        "fees": sum(int(t["fee"]) for t in paid + failed if t.get("fee")),
    }


def short(addr):
    return (addr or "-")[:6]


def charts(a, out_dir):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    files = []
    days = list(a["per_day"])
    fig, ax = plt.subplots(figsize=(9, 3.6))
    ax.bar(days, [v / DROPS for v in a["per_day"].values()], color="#3b6ea5")
    ax.set_ylabel("PFT paid")
    ax.set_title("PFT paid per UTC day (tesSUCCESS outgoing payments, delivered_amount)")
    ax.tick_params(axis="x", rotation=45)
    fig.tight_layout()
    files.append("payouts_per_day.png")
    fig.savefig(os.path.join(out_dir, files[-1]), dpi=110)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(7, 3.6))
    ax.bar(list(a["size_hist"]), list(a["size_hist"].values()), color="#3b6ea5")
    ax.set_xlabel("payment size (PFT)")
    ax.set_ylabel("payments")
    ax.set_title("Payment size distribution")
    ax.tick_params(axis="x", rotation=45)
    fig.tight_layout()
    files.append("payment_sizes.png")
    fig.savefig(os.path.join(out_dir, files[-1]), dpi=110)
    plt.close(fig)

    vals = sorted(a["by_rcpt"].values())
    tot = sum(vals) or 1
    xs, ys, c = [0.0], [0.0], 0
    for i, v in enumerate(vals, 1):
        c += v
        xs.append(i / len(vals))
        ys.append(c / tot)
    fig, ax = plt.subplots(figsize=(4.6, 4.6))
    ax.plot(xs, ys, color="#3b6ea5", label=f"recipients (Gini {a['gini']:.3f})")
    ax.plot([0, 1], [0, 1], color="#999", linestyle="--", label="equal")
    ax.set_xlabel("share of recipients")
    ax.set_ylabel("share of PFT received")
    ax.set_title("Recipient concentration (Lorenz)")
    ax.legend(loc="upper left")
    fig.tight_layout()
    files.append("recipient_lorenz.png")
    fig.savefig(os.path.join(out_dir, files[-1]), dpi=110)
    plt.close(fig)
    return files


def fmt(v, nd=2):
    return "-" if v is None else f"{v:,.{nd}f}"


def render(a, rng, done, account, chart_files):
    hrs = a["per_hour"]
    L = [
        "# rewardflow report: reward-payer PFT outflows",
        "",
        f"- Account: `{short(account)}…` (truncated to 6 chars; full address in the collect log)",
        f"- Ledger range: {rng['ledger_min']}-{rng['ledger_max']} (server complete_ledgers"
        f" `{rng['complete_ledgers']}` at collection start {rng['started']}), RPC {rng['rpc']}",
        f"- Collection: {'complete' if done else 'INCOMPLETE (no done line)'}"
        + (f", {done['tx_count']} account transactions, finished {done['finished']}" if done else ""),
        f"- First / last payment: {a['paid'][0]['close_time'] if a['paid'] else '-'} /"
        f" {a['paid'][-1]['close_time'] if a['paid'] else '-'}",
        "",
        "## Totals",
        "",
        "| metric | value |",
        "|---|---|",
        f"| successful outgoing payments | {a['count']} |",
        f"| PFT paid (sum of delivered_amount) | {fmt(a['total_pft'], 6)} |",
        f"| unique recipients | {a['unique']} |",
        f"| failed outgoing payments (non-tesSUCCESS) | {len(a['failed'])} |",
        f"| partial payments | {a['partial']} |",
        f"| fees paid on outgoing payments (PFT) | {fmt(a['fees'] / DROPS, 6)} |",
        f"| other account transactions (not counted) | {sum(a['other'].values())}"
        f" {dict(a['other']) if a['other'] else ''} |",
        "",
        "## Per day (UTC)",
        "",
        "| day | payments | PFT paid |",
        "|---|---|---|",
    ]
    per_day_n = Counter(t["close_time"][:10] for t in a["paid"])
    for d, v in a["per_day"].items():
        L.append(f"| {d} | {per_day_n[d]} | {fmt(v / DROPS)} |")
    L += [
        "",
        f"Per hour: {len(hrs)} hours with at least one payment; busiest hour "
        + (f"{max(hrs, key=hrs.get)} ({fmt(max(hrs.values()) / DROPS)} PFT)" if hrs else "-")
        + f"; mean over active hours {fmt(sum(hrs.values()) / DROPS / len(hrs) if hrs else None)} PFT.",
        "",
        "## Payment sizes (PFT)",
        "",
        f"p50 {fmt(a['size_pct'][50])} | p90 {fmt(a['size_pct'][90])} | p99 {fmt(a['size_pct'][99])}"
        f" | max {fmt(a['size_max'])}",
        "",
        f"Smallest: {fmt(min(a['sizes']) if a['sizes'] else None, 6)} PFT"
        f" ({sum(1 for s in a['sizes'] if s < 0.001)} payments below 0.001 PFT).",
        "",
        "| size bucket (PFT) | payments |",
        "|---|---|",
    ]
    for k, n in a["size_hist"].items():
        L.append(f"| {k} | {n} |")
    L += [
        "",
        "## Recipient concentration",
        "",
        f"- Top-10 share of PFT: {fmt(100 * a['top10_share'] if a['top10_share'] is not None else None, 1)}%",
        f"- HHI (sum of squared shares, 0-1): {a['hhi']:.4f} (equal split over {a['unique']} recipients"
        f" would be {1 / a['unique'] if a['unique'] else 0:.4f})",
        f"- Gini over recipient totals: {a['gini']:.3f}",
        f"- Repeat-recipient rate (recipients paid more than once): "
        f"{fmt(100 * a['repeat_rate'] if a['repeat_rate'] is not None else None, 1)}%",
        "",
        "| recipient | payments | PFT |",
        "|---|---|---|",
    ]
    for r, v in a["by_rcpt"].most_common(10):
        L.append(f"| `{short(r)}` | {a['cnt_rcpt'][r]} | {fmt(v / DROPS)} |")
    g = a["gap_pct"]
    L += [
        "",
        "## Inter-payment gaps (seconds between consecutive successful payments)",
        "",
        f"n={len(a['gaps'])} | p50 {fmt(g[50], 0)} | p90 {fmt(g[90], 0)} | p99 {fmt(g[99], 0)}"
        f" | max {fmt(a['gap_max'], 0)} ({fmt(a['gap_max'] / 3600 if a['gap_max'] else None, 1)} h)",
        "",
        "## Memos",
        "",
        "Decoded MemoType / MemoFormat, and `kind` = first `<word>_<id>` token found in MemoData"
        " (category only; MemoData itself is not published).",
        "",
        "| MemoType | MemoFormat | kind | payments |",
        "|---|---|---|---|",
    ]
    for (mt, mf, mk), n in a["memo"].most_common():
        L.append(f"| {mt or '(none)'} | {mf or '-'} | {mk or '-'} | {n} |")
    L += ["", "## Charts", ""]
    for f in chart_files:
        L.append(f"![{f}]({f})")
    L += ["", "Generated by `rewardflow/analyze_rewardflow.py`; method and limits in `rewardflow/README.md`.",
          ""]
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("data")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--no-charts", action="store_true")
    a = ap.parse_args(argv)
    rng, done, txs = load(a.data)
    if rng is None:
        print("no range line in data file", file=sys.stderr)
        return 2
    res = analyze(txs, rng["account"])
    os.makedirs(a.out_dir, exist_ok=True)
    files = [] if a.no_charts else charts(res, a.out_dir)
    with open(os.path.join(a.out_dir, "REPORT.md"), "w") as fh:
        fh.write(render(res, rng, done, rng["account"], files))
    print(f"payments={res['count']} pft={res['total_pft']:.6f} recipients={res['unique']}"
          f" failed={len(res['failed'])} partial={res['partial']} -> {a.out_dir}/REPORT.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
