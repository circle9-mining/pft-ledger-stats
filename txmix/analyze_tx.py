#!/usr/bin/env python3
"""Transaction-mix analysis of a collect_tx.py window -> REPORT.md + PNG charts.

Reads tx.csv, ledgers.csv, samples.jsonl, gaps.csv and state.json written by
collect_tx.py and reports:

  tx-type mix        overall and per UTC hour
  result codes       per type, by class (tes/tec/tef/tel/tem/ter) and top codes
  fees               p50/p90/p99/max in drops vs the base fee, share above 10x base
  accounts           active senders, top-10 share, Gini of tx count per sender
  new accounts       AccountRoot objects created (newly funded accounts), by tx type
  memos, multisig    share of transactions carrying Memos / Signers

Transactions are de-duplicated by tx_hash (a crash between the tx.csv and
ledgers.csv appends can repeat a ledger's rows on resume). Ledgers listed in
gaps.csv were not observed; they are reported, not interpolated.

Percentiles use the nearest-rank method (p-th percentile = value at rank
ceil(p/100 * n) of the sorted list), so every reported fee is a fee that was
actually paid. Gini uses the sorted-rank formula
G = sum_i (2i - n - 1) x_i / (n * sum x), i = 1..n ascending.

Usage:
  ./analyze_tx.py --data data/txmix --out txmix/report
"""

import argparse
import csv
import json
import math
import os
import sys
from collections import Counter, defaultdict

DEFAULT_BASE_FEE = 10  # drops; overridden by samples.jsonl base_fee_xrp
TOP_TYPES = 5          # chart shows the top 5 types + "Other" (fixed categorical order)
# Reference categorical palette (light), slots 1-6 in fixed order; "Other" is neutral gray.
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300"]
OTHER = "#8c8c8c"
INK = "#3c3c3a"


def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def dedupe(rows):
    seen, out = set(), []
    for r in rows:
        key = r.get("tx_hash") or (r["ledger_index"], r.get("tx_index"))
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def percentile(sorted_vals, p):
    """Nearest-rank percentile of an ascending list (None if empty)."""
    if not sorted_vals:
        return None
    rank = max(1, math.ceil(p / 100 * len(sorted_vals)))
    return sorted_vals[rank - 1]


def gini(values):
    """Gini coefficient of non-negative values (0 = equal, ->1 = concentrated)."""
    xs = sorted(values)
    n, total = len(xs), sum(xs)
    if n == 0 or total == 0:
        return 0.0
    return sum((2 * i - n - 1) * x for i, x in enumerate(xs, start=1)) / (n * total)


def result_class(code):
    return code[:3] if len(code) >= 3 else "?"


def base_fee_from_samples(path):
    fees = []
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                try:
                    v = json.loads(line).get("base_fee_xrp")
                except json.JSONDecodeError:
                    continue
                if v is not None:
                    fees.append(round(float(v) * 1_000_000))
    return (Counter(fees).most_common(1)[0][0] if fees else DEFAULT_BASE_FEE), sorted(set(fees))


def analyze(txs, ledgers, base_fee):
    """Pure analysis over de-duplicated tx rows and ledger rows; returns a dict."""
    n = len(txs)
    types = Counter(r["tx_type"] for r in txs)
    by_hour = defaultdict(Counter)
    for r in txs:
        by_hour[r["close_time_iso"][:13]][r["tx_type"]] += 1
    for lg in ledgers:  # hours with ledgers but no tx still appear
        by_hour.setdefault(lg["close_time_iso"][:13], Counter())

    results = defaultdict(Counter)
    classes = defaultdict(Counter)
    for r in txs:
        results[r["tx_type"]][r["result"]] += 1
        classes[r["tx_type"]][result_class(r["result"])] += 1

    fees = sorted(int(r["fee_drops"]) for r in txs)
    fee = {
        "n": len(fees),
        "min": fees[0] if fees else None,
        "p50": percentile(fees, 50), "p90": percentile(fees, 90), "p99": percentile(fees, 99),
        "max": fees[-1] if fees else None,
        "above_10x": sum(1 for f in fees if f > 10 * base_fee),
        "at_base": sum(1 for f in fees if f == base_fee),
        "total": sum(fees),
    }
    fee_by_type = {}
    for t in types:
        fs = sorted(int(r["fee_drops"]) for r in txs if r["tx_type"] == t)
        fee_by_type[t] = (percentile(fs, 50), percentile(fs, 99), fs[-1])

    per_acct = Counter(r["account"] for r in txs if r["account"])
    counts = sorted(per_acct.values(), reverse=True)
    top10 = sum(counts[:10])

    created = Counter()
    for r in txs:
        c = int(r["accounts_created"] or 0)
        if c:
            created[r["tx_type"]] += c

    return {
        "n": n, "types": types, "by_hour": dict(sorted(by_hour.items())),
        "results": results, "classes": classes,
        "fee": fee, "fee_by_type": fee_by_type, "base_fee": base_fee,
        "accounts": len(per_acct), "acct_counts": counts,
        "top10_share": (top10 / n) if n else 0.0, "gini": gini(per_acct.values()),
        "created": created, "created_total": sum(created.values()),
        "memo": sum(1 for r in txs if r["memo"] == "y"),
        "memo_by_type": Counter(r["tx_type"] for r in txs if r["memo"] == "y"),
        "multisig": sum(1 for r in txs if int(r["signers"] or 0) > 0),
        "delivered": Counter(r["delivered_class"] for r in txs),
        "ledgers": len(ledgers),
        "empty_ledgers": sum(1 for lg in ledgers if int(lg["txn_count"]) == 0),
    }


def pct(a, b):
    return f"{100 * a / b:.1f}%" if b else "n/a"


def chart_types_by_hour(a, order, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    hours = list(a["by_hour"])
    top = order[:TOP_TYPES]
    series = [(t, SERIES[i]) for i, t in enumerate(top)]
    if len(order) > TOP_TYPES:
        series.append(("Other", OTHER))
    fig, ax = plt.subplots(figsize=(10, 4.2))
    bottom = [0] * len(hours)
    for name, color in series:
        if name == "Other":
            vals = [sum(c for t, c in a["by_hour"][h].items() if t not in top) for h in hours]
        else:
            vals = [a["by_hour"][h].get(name, 0) for h in hours]
        ax.bar(range(len(hours)), vals, bottom=bottom, color=color, label=name,
               edgecolor="white", linewidth=1.0, width=0.85)
        bottom = [b + v for b, v in zip(bottom, vals, strict=True)]
    step = max(1, len(hours) // 12)
    ax.set_xticks(range(0, len(hours), step))
    ax.set_xticklabels([hours[i][5:] + "h" for i in range(0, len(hours), step)], rotation=45, ha="right")
    ax.set_ylabel("transactions / hour", color=INK)
    ax.set_title("Transaction types per hour (UTC)", color=INK)
    ax.grid(axis="y", color="#e4e4e0", linewidth=0.6)
    ax.set_axisbelow(True)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.legend(loc="upper left", bbox_to_anchor=(1.0, 1.0), frameon=False)
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


def chart_fees(txs, base_fee, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fees = Counter(int(r["fee_drops"]) for r in txs)
    xs = sorted(fees)
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.bar(range(len(xs)), [fees[x] for x in xs], color=SERIES[0], width=0.7)
    ax.set_xticks(range(len(xs)))
    ax.set_xticklabels([str(x) for x in xs], rotation=45 if len(xs) > 8 else 0, ha="right")
    ax.set_yscale("log")
    ax.set_xlabel(f"fee paid (drops); base fee = {base_fee} drops", color=INK)
    ax.set_ylabel("transactions (log scale)", color=INK)
    ax.set_title("Fee distribution (each distinct fee value)", color=INK)
    ax.grid(axis="y", color="#e4e4e0", linewidth=0.6)
    ax.set_axisbelow(True)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


def chart_lorenz(counts, g, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    xs = sorted(counts)
    total, n = sum(xs), len(xs)
    cx, cy, run = [0.0], [0.0], 0
    for i, x in enumerate(xs, start=1):
        run += x
        cx.append(i / n)
        cy.append(run / total if total else 0)
    fig, ax = plt.subplots(figsize=(5.5, 5))
    ax.plot([0, 1], [0, 1], color=OTHER, linestyle="--", linewidth=1.2, label="equal activity")
    ax.plot(cx, cy, color=SERIES[0], linewidth=2, label=f"observed (Gini {g:.3f})")
    ax.set_xlabel("share of active sending accounts (ascending activity)", color=INK)
    ax.set_ylabel("share of transactions", color=INK)
    ax.set_title("Sender concentration (Lorenz curve)", color=INK)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.grid(color="#e4e4e0", linewidth=0.6)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.legend(loc="upper left", frameon=False)
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


def render(a, meta):
    n = a["n"]
    order = [t for t, _ in a["types"].most_common()]
    out = []
    w = out.append
    w("# txmix — Post Fiat testnet transaction mix\n")
    w(f"Window: **{meta['start']} → {meta['end']}** (UTC), single node's validated-ledger view, "
      "testnet only.  ")
    w(f"Ledgers observed: **{a['ledgers']}** ({meta['first']}–{meta['last']}), gaps: **{meta['gaps']}**, "
      f"empty ledgers: {a['empty_ledgers']} ({pct(a['empty_ledgers'], a['ledgers'])}).  ")
    w(f"Transactions: **{n}** after de-duplication by tx_hash ({meta['dupes']} duplicate rows dropped); "
      f"{n / a['ledgers']:.2f} tx/ledger.  " if a["ledgers"] else f"Transactions: **{n}**.  ")
    w(f"Base fee: {a['base_fee']} drops (server_info samples: {meta['base_fees']}).\n")
    w("Generated by `txmix/analyze_tx.py` from data collected by `txmix/collect_tx.py`.\n")

    w("## Transaction-type mix\n")
    w("| type | tx | share | tes | tec | other classes | memo | new accounts |")
    w("|---|---:|---:|---:|---:|---:|---:|---:|")
    for t in order:
        c = a["classes"][t]
        other = sum(v for k, v in c.items() if k not in ("tes", "tec"))
        w(f"| {t} | {a['types'][t]} | {pct(a['types'][t], n)} | {c.get('tes', 0)} | {c.get('tec', 0)} | "
          f"{other} | {a['memo_by_type'].get(t, 0)} | {a['created'].get(t, 0)} |")
    w("")
    w("![tx types per hour](charts/types_by_hour.png)\n")

    w("### Per UTC hour\n")
    top = order[:TOP_TYPES]
    head = top + (["Other"] if len(order) > TOP_TYPES else [])
    w("| hour (UTC) | total | " + " | ".join(head) + " |")
    w("|---|---:|" + "---:|" * len(head))
    for h, c in a["by_hour"].items():
        cells = [str(c.get(t, 0)) for t in top]
        if len(order) > TOP_TYPES:
            cells.append(str(sum(v for t, v in c.items() if t not in top)))
        w(f"| {h[5:]}h | {sum(c.values())} | " + " | ".join(cells) + " |")
    w("")

    w("## Result codes\n")
    fails = sum(v for t in a["classes"] for k, v in a["classes"][t].items() if k != "tes")
    w(f"Non-tesSUCCESS results: **{fails}** ({pct(fails, n)}). Validated ledgers only contain tes/tec "
      "results; tef/tel/tem/ter rows would indicate a collector bug and are shown if present.\n")
    w("| type | result | count | share of type |")
    w("|---|---|---:|---:|")
    for t in order:
        for code, c in a["results"][t].most_common():
            w(f"| {t} | {code} | {c} | {pct(c, a['types'][t])} |")
    w("")

    f = a["fee"]
    w("## Fees\n")
    w(f"| p50 | p90 | p99 | max | min | at base fee | above 10× base (> {10 * a['base_fee']}) "
      "| total burned |")
    w("|---:|---:|---:|---:|---:|---:|---:|---:|")
    w(f"| {f['p50']} | {f['p90']} | {f['p99']} | {f['max']} | {f['min']} | {pct(f['at_base'], n)} | "
      f"{f['above_10x']} ({pct(f['above_10x'], n)}) | {f['total']} drops ({f['total'] / 1e6:.6f} PFT) |")
    w("\nDrops; nearest-rank percentiles.\n")
    w("| type | p50 | p99 | max |")
    w("|---|---:|---:|---:|")
    for t in order:
        p50, p99, mx = a["fee_by_type"][t]
        w(f"| {t} | {p50} | {p99} | {mx} |")
    w("")
    w("![fee distribution](charts/fees.png)\n")

    w("## Account activity\n")
    c = a["acct_counts"]
    w(f"Active sending accounts: **{a['accounts']}**. Top-10 senders: **{100 * a['top10_share']:.1f}%** "
      f"of transactions. Gini of tx count per sender: **{a['gini']:.3f}**.  ")
    w(f"Busiest sender: {c[0] if c else 0} tx; median sender: {sorted(c)[len(c) // 2] if c else 0} tx; "
      f"senders with exactly 1 tx: {sum(1 for x in c if x == 1)}.  ")
    w("Accounts are HMAC-SHA256 tokens with a private per-run key, so no addresses are published and tokens "
      "cannot be linked across runs.\n")
    w("| sender rank | tx | share |")
    w("|---:|---:|---:|")
    for i, x in enumerate(c[:10], start=1):
        w(f"| {i} | {x} | {pct(x, n)} |")
    w("")
    w("![Lorenz curve](charts/lorenz.png)\n")

    w("## Newly funded accounts, memos, multisig, delivered amounts\n")
    w(f"- New AccountRoot objects created: **{a['created_total']}** "
      + (f"({', '.join(f'{t} {v}' for t, v in a['created'].most_common())})" if a["created"] else "") + ".")
    w(f"- Transactions with Memos: **{a['memo']}** ({pct(a['memo'], n)}).")
    w(f"- Multi-signed transactions (Signers present): **{a['multisig']}** ({pct(a['multisig'], n)}).")
    w("- delivered_amount class: " + ", ".join(f"{k} {v}" for k, v in a["delivered"].most_common()) + ".\n")

    w("## Limits\n")
    w("- One node's view of validated ledgers on the Post Fiat **testnet**; mainnet behaviour may differ.")
    w("- Only validated, included transactions are seen: rejected/queued transactions (tef/tel/tem/ter) "
      "never reach a ledger.")
    w("- `account` is the sending account only; destinations are not recorded, so the Gini is sender-side.")
    w("- Chart colours: reference categorical palette slots 1-6 in fixed order, 'Other' in neutral gray; "
      "every chart has its numbers in a table above.")
    return "\n".join(out) + "\n"


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data", default="data/txmix", help="collect_tx.py output directory")
    p.add_argument("--out", default="txmix/report", help="directory for REPORT.md and charts/")
    p.add_argument("--no-charts", action="store_true")
    args = p.parse_args(argv)

    raw = read_csv(os.path.join(args.data, "tx.csv"))
    txs = dedupe(raw)
    ledgers = read_csv(os.path.join(args.data, "ledgers.csv"))
    gaps = read_csv(os.path.join(args.data, "gaps.csv"))
    if not ledgers:
        print("no ledgers.csv rows in " + args.data, file=sys.stderr)
        return 2
    base_fee, base_fees = base_fee_from_samples(os.path.join(args.data, "samples.jsonl"))
    state = {}
    sp = os.path.join(args.data, "state.json")
    if os.path.exists(sp):
        with open(sp) as f:
            state = json.load(f)

    a = analyze(txs, ledgers, base_fee)
    idx = sorted(int(lg["ledger_index"]) for lg in ledgers)
    meta = {
        "start": state.get("start_utc", ledgers[0]["close_time_iso"]),
        "end": state.get("end_utc", ledgers[-1]["close_time_iso"]),
        "first": idx[0], "last": idx[-1], "gaps": len(gaps), "dupes": len(raw) - len(txs),
        "base_fees": ", ".join(str(x) for x in base_fees) or "none, default used",
    }
    os.makedirs(args.out, exist_ok=True)
    if not args.no_charts:
        charts = os.path.join(args.out, "charts")
        os.makedirs(charts, exist_ok=True)
        order = [t for t, _ in a["types"].most_common()]
        chart_types_by_hour(a, order, os.path.join(charts, "types_by_hour.png"))
        chart_fees(txs, base_fee, os.path.join(charts, "fees.png"))
        chart_lorenz(a["acct_counts"], a["gini"], os.path.join(charts, "lorenz.png"))
    with open(os.path.join(args.out, "REPORT.md"), "w") as f:
        f.write(render(a, meta))
    missing = (idx[-1] - idx[0] + 1) - len(set(idx))
    print(f"ledgers {len(ledgers)} range {idx[0]}-{idx[-1]} missing_in_range {missing} gaps.csv {len(gaps)}")
    print(f"tx {len(txs)} (raw {len(raw)}, dupes {len(raw) - len(txs)}) types {len(a['types'])} "
          f"accounts {a['accounts']} gini {a['gini']:.3f} top10 {100 * a['top10_share']:.1f}%")
    f = a["fee"]
    print(f"fees p50 {f['p50']} p90 {f['p90']} p99 {f['p99']} max {f['max']} above_10x {f['above_10x']} "
          f"base {base_fee}")
    print(f"wrote {os.path.join(args.out, 'REPORT.md')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
