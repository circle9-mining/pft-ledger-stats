#!/usr/bin/env python3
"""Build REPORT.md and PNG charts from data/ledgers.csv and data/samples.jsonl."""

import argparse
import csv
import os
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from pftstats import stats  # noqa: E402


def fmt(v):
    return f"{v:.2f}" if isinstance(v, float) else str(v)


def chart_intervals(vals, path):
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.hist(vals, bins=range(0, max(vals) + 2), color="#3b6ea5", edgecolor="white")
    ax.set_yscale("log")
    ax.set_xlabel("close_time delta between consecutive ledgers (s)")
    ax.set_ylabel("ledgers (log scale)")
    ax.set_title("Ledger close-interval distribution")
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


def chart_hourly(hours, path):
    labels = list(hours)
    fig, ax1 = plt.subplots(figsize=(10, 4))
    ax1.bar(range(len(labels)), [hours[h]["txns"] for h in labels], color="#3b6ea5", label="transactions")
    ax1.set_ylabel("transactions / hour")
    ax2 = ax1.twinx()
    ax2.plot(range(len(labels)), [hours[h]["ledgers"] for h in labels], color="#c0504d", marker=".",
             label="ledgers")
    ax2.set_ylabel("ledgers / hour")
    step = max(1, len(labels) // 12)
    ax1.set_xticks(range(0, len(labels), step))
    ax1.set_xticklabels([labels[i][5:] + "h" for i in range(0, len(labels), step)], rotation=45, ha="right")
    ax1.set_title("Transactions and ledgers per hour (UTC)")
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


def chart_quorum(margin, path):
    fig, ax = plt.subplots(figsize=(10, 4))
    x = range(len(margin))
    ax.plot(x, [m[1] for m in margin], label="last_close.proposers", color="#3b6ea5")
    ax.plot(x, [m[2] for m in margin], label="validation_quorum", color="#c0504d", linestyle="--")
    step = max(1, len(margin) // 12)
    ax.set_xticks(range(0, len(margin), step))
    ax.set_xticklabels([margin[i][0][5:16] for i in range(0, len(margin), step)], rotation=45, ha="right")
    ax.set_ylabel("validators")
    ax.set_ylim(bottom=0)
    ax.legend(loc="lower right")
    ax.set_title("Proposers vs validation quorum (5-min samples)")
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data", default="data")
    p.add_argument("--out", default="REPORT.md")
    p.add_argument("--charts", default="charts")
    p.add_argument("--crosscheck", default=None, help="optional crosscheck CSV to include")
    a = p.parse_args(argv)

    rows = stats.load_ledgers(os.path.join(a.data, "ledgers.csv"))
    samples = stats.load_samples(os.path.join(a.data, "samples.jsonl"))
    skips_path = os.path.join(a.data, "skips.csv")
    skips = []
    if os.path.exists(skips_path):
        with open(skips_path, newline="") as fh:
            skips = [r["ledger_index"] for r in csv.DictReader(fh)]
    os.makedirs(a.charts, exist_ok=True)

    gaps = stats.find_gaps(rows)
    iv = stats.close_intervals(rows)
    vals = [i[2] for i in iv]
    summ = stats.summarize(vals)
    limit, stall_list = stats.stalls(iv)
    res_floor = max(r["close_time_resolution"] for r in rows)
    rlimit, rstall_list = stats.stalls(iv, floor=res_floor)
    hours = stats.by_hour(rows)
    burn_total, burn_per = stats.fee_burn(rows)
    margin = stats.quorum_margin(samples)
    txns = [r["txn_count"] for r in rows]
    span_s = rows[-1]["close_time"] - rows[0]["close_time"]
    resolutions = sorted({r["close_time_resolution"] for r in rows})
    full_hours = [h for h in list(hours)[1:-1]]  # first/last hour are partial

    chart_intervals(vals, os.path.join(a.charts, "close_intervals.png"))
    chart_hourly(hours, os.path.join(a.charts, "hourly.png"))
    chart_quorum(margin, os.path.join(a.charts, "quorum.png"))

    L = []
    L.append("# Post Fiat testnet ledger health report\n")
    L.append(f"Single-node view, collected by `collect.py`, built by `analyze.py` from `{a.data}/`.\n")
    L.append("## Coverage\n")
    L.append("| item | value |\n|---|---|")
    L.append(f"| ledger range | {rows[0]['ledger_index']} – {rows[-1]['ledger_index']} |")
    L.append(f"| ledgers collected | {len(rows)} |")
    L.append(f"| first close (UTC) | {rows[0]['close_time_iso']} |")
    L.append(f"| last close (UTC) | {rows[-1]['close_time_iso']} |")
    L.append(f"| span | {span_s} s ({span_s / 3600:.2f} h) |")
    L.append(f"| gaps in ledger_index | {len(gaps)} {gaps if gaps else ''} |")
    L.append(f"| skipped ledgers (skips.csv) | {len(skips)} {skips if skips else ''} |")
    L.append(f"| 5-min samples | {len(samples)} |")
    L.append(f"| close_time_resolution values seen | {resolutions} s |\n")

    L.append("## Close intervals\n")
    L.append("`close_time` is rounded to `close_time_resolution`, so individual deltas are coarse (see "
             "README *Limits*). The mean is still accurate over a long window because rounding errors do "
             "not accumulate: mean = span / (ledgers − 1).\n")
    L.append("| min | median | p95 | p99 | max | mean |\n|---|---|---|---|---|---|")
    L.append(f"| {summ['min']} | {fmt(summ['median'])} | {fmt(summ['p95'])} | {fmt(summ['p99'])} | "
             f"{summ['max']} | {summ['mean']:.3f} |\n")
    L.append("![close interval distribution](charts/close_intervals.png)\n")

    L.append("## Stalls\n")
    L.append(f"Literal rule (card): interval > 3 × median raw delta = **{fmt(limit)} s** → "
             f"{len(stall_list)} interval(s). The median raw delta is distorted by close_time rounding, so "
             f"a resolution-aware rule is also reported: interval > 3 × max(median, close_time_resolution "
             f"{res_floor} s) = **{fmt(rlimit)} s** → {len(rstall_list)} interval(s).\n")
    for title, lst in (("Resolution-aware stalls", rstall_list), ("Literal 3 × median stalls", stall_list)):
        L.append(f"### {title} ({len(lst)})\n")
        if not lst:
            L.append("None.\n")
            continue
        L.append("| ledger | close (UTC) | interval s |\n|---|---|---|")
        for idx, iso, sec in sorted(lst, key=lambda x: (-x[2], x[0]))[:50]:
            L.append(f"| {idx} | {iso} | {sec} |")
        if len(lst) > 50:
            L.append(f"\n(top 50 of {len(lst)} shown, longest first; full list: `stalls.csv`)")
        L.append("")
    with open(os.path.join(os.path.dirname(os.path.abspath(a.out)), "stalls.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ledger_index", "close_time_iso", "interval_s", "rule"])
        for rule, lst in (("resolution_aware", rstall_list), ("literal_3x_median", stall_list)):
            for idx, iso, sec in lst:
                w.writerow([idx, iso, sec, rule])

    L.append("## Transactions\n")
    L.append(f"Total {sum(txns)} transactions in {len(rows)} ledgers; "
             f"{sum(1 for t in txns if t == 0)} ledgers were empty.\n")
    ts = stats.summarize(txns)
    L.append("| per ledger | min | median | p95 | p99 | max | mean |\n|---|---|---|---|---|---|---|")
    L.append(f"| txns | {ts['min']} | {fmt(ts['median'])} | {fmt(ts['p95'])} | {fmt(ts['p99'])} | "
             f"{ts['max']} | {ts['mean']:.3f} |\n")
    if full_hours:
        th = stats.summarize([hours[h]["txns"] for h in full_hours])
        lh = stats.summarize([hours[h]["ledgers"] for h in full_hours])
        L.append(f"Full hours only ({len(full_hours)}):\n")
        L.append("| per hour | min | median | max | mean |\n|---|---|---|---|---|")
        L.append(f"| txns | {th['min']} | {fmt(th['median'])} | {th['max']} | {th['mean']:.1f} |")
        L.append(f"| ledgers | {lh['min']} | {fmt(lh['median'])} | {lh['max']} | {lh['mean']:.1f} |\n")
    L.append("![transactions and ledgers per hour](charts/hourly.png)\n")
    L.append("<details><summary>Hourly table</summary>\n")
    L.append("| hour (UTC) | ledgers | txns | burn (drops) |\n|---|---|---|---|")
    for h, b in hours.items():
        L.append(f"| {h} | {b['ledgers']} | {b['txns']} | {b['burn_drops']} |")
    L.append("\n</details>\n")

    L.append("## Fee burn (total_coins)\n")
    L.append(f"total_coins {rows[0]['total_coins']} → {rows[-1]['total_coins']} drops: "
             f"**{burn_total} drops ({burn_total / stats.DROPS_PER_PFT:.6f} PFT) burned** over the window; "
             f"{sum(1 for b in burn_per if b > 0)} ledgers burned fees, "
             f"max in one ledger {max(burn_per)} drops.\n")

    L.append("## Proposers vs quorum\n")
    if margin:
        ms = [m[3] for m in margin]
        L.append(f"{len(margin)} samples. proposers min {min(m[1] for m in margin)} / max "
                 f"{max(m[1] for m in margin)}; quorum min {min(m[2] for m in margin)} / max "
                 f"{max(m[2] for m in margin)}; margin (proposers − quorum) min {min(ms)}, median "
                 f"{fmt(stats.percentile(ms, 50))}; samples below quorum: {sum(1 for m in ms if m < 0)}. "
                 f"Trusted validators (UNL size) seen: {sorted({s['trusted_validators'] for s in samples})}. "
                 "Non-`full` server_state samples: "
                 f"{sum(1 for s in samples if s['server_state'] != 'full')}.\n")
    L.append("![proposers vs quorum](charts/quorum.png)\n")

    if a.crosscheck and os.path.exists(a.crosscheck):
        with open(a.crosscheck, newline="") as fh:
            cc = list(csv.DictReader(fh))
        L.append("## Cross-check against a second node (sentry)\n")
        L.append("| ledger | hash match | close_time match |\n|---|---|---|")
        for r in cc:
            L.append(f"| {r['ledger_index']} | {r['hash_match']} | {r['close_time_match']} |")
        L.append("")

    with open(a.out, "w") as fh:
        fh.write("\n".join(L) + "\n")
    print(f"wrote {a.out}: {len(rows)} ledgers, {len(samples)} samples, {len(stall_list)} stalls")
    return 0


if __name__ == "__main__":
    sys.exit(main())
