#!/usr/bin/env python3
"""Analyze a txflow flow.jsonl: proposed->validated latency, never-validated share, fee/queue behaviour.

Usage: analyze_flow.py FLOW_JSONL --report DIR [--n-ledgers 20] [--charts]
Writes DIR/REPORT.md and DIR/summary.json (and DIR/*.png with --charts).

Definitions (single vantage point: this node's stream, this host's clock):
  latency_s   t(first validated message) - t(first proposed message), both read by the collector
  latency_l   validated ledger_index - ledger_current_index at first proposed sighting
              (0 = included in the open ledger it was first seen in)
  outcome of a proposed hash, judged N ledgers after its ledger_current_index:
    validated       validated within N ledgers
    late            validated, but after more than N ledgers
    never           not validated, every ledger cur..cur+N observed (no gap) -> counted
    unknown_gap     not validated and a gap (stream skip / reconnect / restart) overlaps cur..cur+N
    pending_end     the dataset ends before cur+N
  never-validated share per engine_result class = never / (validated + late + never)
  fee escalation: a fee sample whose open_ledger_fee > base_fee
"""

import argparse
import json
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from pftstats.stats import percentile  # noqa: E402

OUTCOMES = ["validated", "late", "never", "unknown_gap", "pending_end"]


def load(path):
    evs, bad = [], 0
    with open(path) as fh:
        for line in fh:
            try:
                evs.append(json.loads(line))
            except ValueError:
                bad += 1
    return evs, bad


def build(evs):
    """Per-hash first proposed / first validated, plus observed ledgers, gap ledgers and fee samples."""
    prop, val = {}, {}
    ledgers, gap_ledgers, fees, gaps = set(), set(), [], []
    for e in evs:
        ev = e.get("ev")
        if ev == "proposed":
            prop.setdefault(e["hash"], e)
        elif ev == "validated":
            val.setdefault(e["hash"], e)
        elif ev == "ledger":
            ledgers.add(e["ledger_index"])
        elif ev == "gap":
            gaps.append(e)
            gap_ledgers.update(range(e["from"], e["to"] + 1))
        elif ev == "fee":
            fees.append(e)
    return {
        "prop": prop,
        "val": val,
        "ledgers": ledgers,
        "gap_ledgers": gap_ledgers,
        "fees": fees,
        "gaps": gaps,
    }


def er_class(er):
    return (er or "none")[:3]


def classify(d, n):
    """Outcome per proposed hash (see module doc)."""
    last = max(d["ledgers"]) if d["ledgers"] else None
    out = {}
    for h, p in d["prop"].items():
        cur = p.get("ledger_current_index")
        v = d["val"].get(h)
        if v is not None:
            out[h] = "validated" if cur is None or v["ledger_index"] - cur <= n else "late"
        elif cur is None or last is None or last < cur + n:
            out[h] = "pending_end"
        elif any(i in d["gap_ledgers"] for i in range(cur, cur + n + 1)):
            out[h] = "unknown_gap"
        else:
            out[h] = "never"
    return out


def latencies(d):
    rows = []
    for h, p in d["prop"].items():
        v = d["val"].get(h)
        if v is None:
            continue
        cur = p.get("ledger_current_index")
        rows.append(
            {
                "hash": h,
                "tx_type": v.get("tx_type") or p.get("tx_type"),
                "s": v["t"] - p["t"],
                "l": (v["ledger_index"] - cur) if cur is not None else None,
            }
        )
    return rows


def dist(values):
    if not values:
        return None
    return {
        "count": len(values),
        "p50": percentile(values, 50),
        "p90": percentile(values, 90),
        "p99": percentile(values, 99),
        "max": max(values),
    }


def fee_periods(fees):
    """Consecutive-ledger runs of escalated fee samples."""
    periods, cur = [], None
    for f in sorted(fees, key=lambda x: x["ledger_index"]):
        esc = f["open_ledger_fee"] > f["base_fee"]
        if esc and cur and f["ledger_index"] == cur["end"] + 1:
            cur["end"] = f["ledger_index"]
            cur["t_end"] = f["t"]
            cur["max_open_ledger_fee"] = max(cur["max_open_ledger_fee"], f["open_ledger_fee"])
            cur["max_queue"] = max(cur["max_queue"], f["queue"])
        elif esc:
            cur = {
                "start": f["ledger_index"],
                "end": f["ledger_index"],
                "t_start": f["t"],
                "t_end": f["t"],
                "max_open_ledger_fee": f["open_ledger_fee"],
                "max_queue": f["queue"],
            }
            periods.append(cur)
        else:
            cur = None
    return periods


def analyze(evs, n):
    d = build(evs)
    oc = classify(d, n)
    lat = latencies(d)
    by_type = defaultdict(list)
    by_type_l = defaultdict(list)
    for r in lat:
        by_type[r["tx_type"]].append(r["s"])
        if r["l"] is not None:
            by_type_l[r["tx_type"]].append(r["l"])
    cls = defaultdict(Counter)
    for h, o in oc.items():
        cls[er_class(d["prop"][h].get("engine_result"))][o] += 1
    share = {}
    for c, cnt in cls.items():
        denom = cnt["validated"] + cnt["late"] + cnt["never"]
        share[c] = round(cnt["never"] / denom, 4) if denom else None
    fees = d["fees"]
    queue = [f["queue"] for f in fees]
    sessions = [e for e in evs if e.get("ev") == "session"]
    return {
        "n_ledgers": n,
        "ledgers_observed": len(d["ledgers"]),
        "ledger_range": [min(d["ledgers"]), max(d["ledgers"])] if d["ledgers"] else None,
        "gaps": [{k: g[k] for k in ("from", "to", "count", "reason")} for g in d["gaps"]],
        "gap_ledgers": len(d["gap_ledgers"]),
        "sessions": len(sessions),
        "proposed": len(d["prop"]),
        "validated": len(d["val"]),
        "validated_without_proposed": sum(1 for h in d["val"] if h not in d["prop"]),
        "outcomes": dict(Counter(oc.values())),
        "by_engine_result_class": {c: dict(cnt) for c, cnt in sorted(cls.items())},
        "never_share": share,
        "never_hashes": sorted(h for h, o in oc.items() if o == "never"),
        "latency_s": dist([r["s"] for r in lat]),
        "latency_l": dist([r["l"] for r in lat if r["l"] is not None]),
        "latency_l_counts": dict(sorted(Counter(r["l"] for r in lat if r["l"] is not None).items())),
        "latency_s_by_type": {t: dist(v) for t, v in sorted(by_type.items(), key=lambda kv: -len(kv[1]))},
        "latency_l_by_type": {t: dist(v) for t, v in sorted(by_type_l.items(), key=lambda kv: -len(kv[1]))},
        "fee_samples": len(fees),
        "queue": dist(queue),
        "ledgers_with_queue": sum(1 for q in queue if q > 0),
        "escalated_samples": sum(1 for f in fees if f["open_ledger_fee"] > f["base_fee"]),
        "escalation_periods": fee_periods(fees),
        "max_open_ledger_fee": max((f["open_ledger_fee"] for f in fees), default=None),
        "base_fee_values": sorted({f["base_fee"] for f in fees}),
    }


def fmt(x, nd=2):
    if x is None:
        return "-"
    return f"{x:.{nd}f}" if isinstance(x, float) else str(x)


def dist_row(name, dd, nd=2):
    if not dd:
        return f"| {name} | 0 | - | - | - | - |"
    return (
        f"| {name} | {dd['count']} | {fmt(dd['p50'], nd)} | {fmt(dd['p90'], nd)} | "
        f"{fmt(dd['p99'], nd)} | {fmt(dd['max'], nd)} |"
    )


def render(s, title="txflow report"):
    L = [f"# {title}", ""]
    L += [
        f"- ledgers observed: {s['ledgers_observed']} (range {s['ledger_range']}),"
        f" gap ledgers: {s['gap_ledgers']} in {len(s['gaps'])} gap(s), collector sessions: {s['sessions']}",
        f"- proposed hashes: {s['proposed']}, validated hashes: {s['validated']}"
        f" (validated without a proposed sighting: {s['validated_without_proposed']})",
        f"- outcomes (N = {s['n_ledgers']} ledgers): "
        + ", ".join(f"{o} {s['outcomes'].get(o, 0)}" for o in OUTCOMES),
        "",
        "## Latency, proposed -> validated",
        "",
        "| scope | count | p50 | p90 | p99 | max |",
        "|---|---:|---:|---:|---:|---:|",
        dist_row("all, seconds", s["latency_s"]),
        dist_row("all, ledgers", s["latency_l"], 0),
    ]
    L += [dist_row(f"{t}, seconds", dd) for t, dd in s["latency_s_by_type"].items()]
    L += [dist_row(f"{t}, ledgers", dd, 0) for t, dd in s["latency_l_by_type"].items()]
    L += [
        "",
        "Ledgers to validation (count of txs): "
        + ", ".join(f"{k}: {v}" for k, v in s["latency_l_counts"].items()),
    ]
    L += ["", "## Outcome by engine_result class (at first proposed sighting)", ""]
    L += [
        "| class | " + " | ".join(OUTCOMES) + " | never-validated share |",
        "|---|" + "---:|" * (len(OUTCOMES) + 1),
    ]
    for c, cnt in s["by_engine_result_class"].items():
        sh = s["never_share"].get(c)
        L.append(
            f"| {c} | "
            + " | ".join(str(cnt.get(o, 0)) for o in OUTCOMES)
            + f" | {'-' if sh is None else f'{sh * 100:.2f}%'} |"
        )
    L += ["", "## Fee and queue (one fee sample per validated ledger)", ""]
    q = s["queue"] or {}
    L += [
        f"- fee samples: {s['fee_samples']}, base_fee values: {s['base_fee_values']},"
        f" max open_ledger_fee: {s['max_open_ledger_fee']}",
        f"- queue size p50/p90/p99/max: {fmt(q.get('p50'))}/{fmt(q.get('p90'))}/{fmt(q.get('p99'))}"
        f"/{q.get('max', '-')};"
        f" samples with a non-empty queue: {s['ledgers_with_queue']}",
        f"- escalated samples (open_ledger_fee > base_fee): {s['escalated_samples']}"
        f" in {len(s['escalation_periods'])} period(s)",
    ]
    if s["escalation_periods"]:
        L += ["", "| ledgers | samples | max open_ledger_fee | max queue |", "|---|---:|---:|---:|"]
        for p in s["escalation_periods"]:
            L.append(
                f"| {p['start']}-{p['end']} | {p['end'] - p['start'] + 1} |"
                f" {p['max_open_ledger_fee']} | {p['max_queue']} |"
            )
    if s["gaps"]:
        L += ["", "## Gaps", "", "| from | to | count | reason |", "|---:|---:|---:|---|"]
        L += [f"| {g['from']} | {g['to']} | {g['count']} | {g['reason']} |" for g in s["gaps"]]
    return "\n".join(L) + "\n"


SURFACE, INK, MUTED, SERIES = "#fcfcfb", "#0b0b0b", "#52514e", "#2a78d6"


def _axes(ax):
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(MUTED)
    ax.tick_params(colors=MUTED, labelsize=9)
    ax.grid(axis="y", color="#e4e3df", linewidth=0.8)
    ax.set_axisbelow(True)


def charts(evs, s, outdir):
    """latency.png (histogram, seconds) and queue_fee.png (two stacked panels sharing time, one axis each)."""
    import matplotlib

    matplotlib.use("Agg")
    from datetime import datetime, timezone

    import matplotlib.pyplot as plt
    from matplotlib.ticker import MaxNLocator

    d = build(evs)
    lat = [r["s"] for r in latencies(d)]
    files = []
    if lat:
        fig, ax = plt.subplots(figsize=(8, 3.6), facecolor=SURFACE)
        _axes(ax)
        hi = percentile(lat, 99.5)
        ax.hist([min(x, hi) for x in lat], bins=40, color=SERIES, edgecolor=SURFACE, linewidth=2)
        ax.set_xlabel(
            "seconds from first proposed sighting to validated (values above p99.5 put in last bin)",
            color=MUTED,
            fontsize=9,
        )
        ax.set_ylabel("transactions", color=MUTED, fontsize=9)
        ax.yaxis.set_major_locator(MaxNLocator(integer=True))
        ax.set_title(
            f"Proposed -> validated latency (n={len(lat)}, p50 {s['latency_s']['p50']:.2f} s,"
            f" p99 {s['latency_s']['p99']:.2f} s)",
            color=INK,
            fontsize=11,
            loc="left",
        )
        fig.tight_layout()
        fig.savefig(os.path.join(outdir, "latency.png"), dpi=110)
        plt.close(fig)
        files.append("latency.png")
    fees = sorted(d["fees"], key=lambda f: f["t"])
    if fees:
        ts = [datetime.fromtimestamp(f["t"], timezone.utc) for f in fees]
        fig, (a1, a2) = plt.subplots(2, 1, figsize=(8, 4.8), sharex=True, facecolor=SURFACE)
        for ax, key, label in (
            (a1, "queue", "queued transactions"),
            (a2, "open_ledger_fee", "open-ledger fee (drops)"),
        ):
            _axes(ax)
            vals = [f[key] for f in fees]
            ax.step(ts, vals, where="post", color=SERIES, linewidth=2)
            ax.set_ylabel(label, color=MUTED, fontsize=9)
            top = max(vals)
            ax.set_ylim(0, top * 1.1 if top > 0 else 1)  # an all-zero series would get a fractional autoscale
        a1.yaxis.set_major_locator(MaxNLocator(integer=True))
        a1.set_title(
            "Transaction queue and open-ledger fee, one sample per validated ledger (UTC)",
            color=INK,
            fontsize=11,
            loc="left",
        )
        fig.autofmt_xdate()
        fig.tight_layout()
        fig.savefig(os.path.join(outdir, "queue_fee.png"), dpi=110)
        plt.close(fig)
        files.append("queue_fee.png")
    return files


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("flow")
    ap.add_argument("--report", required=True)
    ap.add_argument("--n-ledgers", type=int, default=20)
    ap.add_argument("--title", default="txflow report")
    ap.add_argument(
        "--charts", action="store_true", help="also write latency.png and queue_fee.png (matplotlib)"
    )
    a = ap.parse_args(argv)
    evs, bad = load(a.flow)
    s = analyze(evs, a.n_ledgers)
    s["bad_lines"] = bad
    os.makedirs(a.report, exist_ok=True)
    with open(os.path.join(a.report, "summary.json"), "w") as fh:
        json.dump(s, fh, indent=1, sort_keys=True)
    md = render(s, a.title)
    if a.charts:
        md += "\n## Charts\n\n" + "".join(f"![{f}]({f})\n\n" for f in charts(evs, s, a.report))
    with open(os.path.join(a.report, "REPORT.md"), "w") as fh:
        fh.write(md)
    print(render(s, a.title))
    return 0


if __name__ == "__main__":
    sys.exit(main())
