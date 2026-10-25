#!/usr/bin/env python3
"""Analyze a vlatency vl.jsonl: per-validator receive latency, signing-clock skew, late arrivals.

Usage: analyze_vlatency.py VL_JSONL --report DIR [--late 2.0] [--charts] [--title T]
Writes DIR/REPORT.md, DIR/summary.json, DIR/validators.csv (and DIR/*.png with --charts).

Definitions (single vantage point: this node's streams, this host's clock; t = local receive time):
  ledger L      a `ledger` event (validated ledger, hash lh, close_time ct in Ripple-epoch seconds)
  validation v  first sighting of (signing key, ledger index, ledger hash), deduplicated over the file
  matched v     v.lh == lh of the validated ledger at v.li ("on the validated hash")
  d_first       v.t - min(t of matched validations of L)  receive delay behind the fastest validator
                (resolution-free; the primary latency metric)
  d_closed      v.t - t(ledgerClosed of L)  negative = arrived before this node announced L validated
                (validations arrive first: L is validated once a quorum of them is in)
  d_ct          v.t - unix(L.ct)  latency vs the ledger's network close time. close_time is rounded
                to close_time_resolution (10 s on this testnet; rippled's set is 10/20/30/60/90/120 s)
                and then bumped to prev_ct + 1 s when the rounded value does not move forward, so a
                single d_ct is only good to about +-res/2. Reported only over ledgers whose ct is a
                multiple of the resolution ("rounded"), never over bumped ones.
  skew          unix(v.st) - v.t  signing_time (whole Ripple seconds, truncated) minus receive time.
                Network transit and truncation both push it negative: about -0.5 s .. -1.5 s is
                normal; a validator far outside the network median has a clock (or close-time
                offset) problem, or is slow to relay.
  late          d_first > --late seconds
  non-validated a validation for a ledger index this node saw validated, but for a different hash
  active span   a validator's first..last matched ledger index; coverage = matched ledgers /
                observed ledgers inside its span (ledgers inside gap events have no ledger event,
                so they are never counted as missed)
"""

import argparse
import csv
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from pftstats.stats import percentile  # noqa: E402

RIPPLE_EPOCH = 946684800  # 2000-01-01T00:00:00Z in unix seconds


def ripple_to_unix(rt):
    return rt + RIPPLE_EPOCH


def round_close_time(t, res):
    """rippled roundCloseTime: add res/2, cut to a multiple of res (Ripple-epoch seconds)."""
    if t == 0:
        return 0
    t += res // 2
    return t - (t % res)


def eff_close_time(t, res, prior):
    """rippled effCloseTime: the rounded time, but at least prior + 1 s."""
    if t == 0:
        return 0
    return max(round_close_time(t, res), prior + 1)


def load(path):
    evs, bad = [], 0
    with open(path) as fh:
        for line in fh:
            try:
                evs.append(json.loads(line))
            except ValueError:
                bad += 1
    return evs, bad


def build(evs, res=10):
    """Ledgers, deduplicated validations and gaps. Event order in the file does not matter."""
    ledgers, vals, seen, gaps, sessions, dups = {}, [], set(), [], [], 0
    for e in evs:
        ev = e.get("ev")
        if ev == "ledger":
            ledgers.setdefault(e["li"], e)
        elif ev == "val":
            key = (e["k"], e["li"], e["lh"])
            if key in seen:
                dups += 1
                continue
            seen.add(key)
            vals.append(e)
        elif ev == "gap":
            gaps.append(e)
        elif ev == "session":
            sessions.append(e)
    prev = None
    for li in sorted(ledgers):
        L = ledgers[li]
        ct = L.get("ct")
        if ct is None:
            L["ct_kind"] = "missing"
        elif ct % res == 0:
            L["ct_kind"] = "rounded"
        elif prev and prev.get("ct") is not None and prev["li"] == li - 1 and ct == prev["ct"] + 1:
            L["ct_kind"] = "bumped"
        else:
            L["ct_kind"] = "other"
        prev = L
    return {"ledgers": ledgers, "vals": vals, "gaps": gaps, "sessions": sessions, "dups": dups}


def dist(values, nd=3):
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "p50": round(percentile(values, 50), nd),
        "p90": round(percentile(values, 90), nd),
        "p99": round(percentile(values, 99), nd),
        "max": round(max(values), nd),
        "min": round(min(values), nd),
    }


def hour(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:00Z")


def analyze(evs, late=2.0, res=10):
    d = build(evs, res)
    led, vals = d["ledgers"], d["vals"]
    first = {}
    for v in vals:
        L = led.get(v["li"])
        if L and L.get("lh") == v["lh"]:
            first[v["li"]] = min(first.get(v["li"], v["t"]), v["t"])
    per = defaultdict(lambda: defaultdict(list))
    hourly = defaultdict(list)
    nonval, unmatched, partial, all_first, all_skew = defaultdict(int), 0, 0, [], []
    for v in vals:
        k = v["k"]
        per[k]["mk"] = v.get("mk") or per[k].get("mk")
        if not v.get("full", True):
            partial += 1
            per[k]["partial"].append(1)
        L = led.get(v["li"])
        if L is None:
            unmatched += 1  # ledger not seen (gap, window edge, or ledger never validated here)
            continue
        if L.get("lh") != v["lh"]:
            nonval[k] += 1
            continue
        df = v["t"] - first[v["li"]]
        per[k]["d_first"].append(df)
        per[k]["d_closed"].append(v["t"] - L["t"])
        if L.get("ct_kind") == "rounded":
            per[k]["d_ct"].append(v["t"] - ripple_to_unix(L["ct"]))
        if v.get("st") is not None:
            sk = ripple_to_unix(v["st"]) - v["t"]
            per[k]["skew"].append(sk)
            all_skew.append(sk)
        per[k]["li"].append(v["li"])
        all_first.append(df)
        hourly[hour(L["t"])].append(df)
    observed = sorted(led)
    net_skew = percentile(all_skew, 50) if all_skew else None
    rows = []
    for k, p in per.items():
        lis = sorted(set(p["li"]))
        if lis:
            span = [i for i in observed if lis[0] <= i <= lis[-1]]
            cov = len(lis) / len(span) if span else None
        else:
            cov = None
        df = p["d_first"]
        r = {
            "key": k,
            "master_key": p.get("mk"),
            "matched": len(df),
            "first_ledger": lis[0] if lis else None,
            "last_ledger": lis[-1] if lis else None,
            "span_coverage": round(cov, 4) if cov is not None else None,
            "window_coverage": round(len(lis) / len(observed), 4) if observed else None,
            "d_first": dist(df),
            "d_closed": dist(p["d_closed"]),
            "d_ct": dist(p["d_ct"]),
            "skew": dist(p["skew"]),
            "skew_vs_network": round(percentile(p["skew"], 50) - net_skew, 3) if p["skew"] else None,
            "late_share": round(sum(1 for x in df if x > late) / len(df), 4) if df else None,
            "non_validated": nonval.get(k, 0),
            "partial": len(p["partial"]),
        }
        rows.append(r)
    rows.sort(key=lambda r: (r["d_first"].get("p50", 1e9), r["key"]))
    kinds = defaultdict(int)
    for L in led.values():
        kinds[L["ct_kind"]] += 1
    ts = sorted(L["t"] for L in led.values())
    s = {
        "late_threshold_s": late,
        "close_time_resolution_s": res,
        "first_ledger_t": ts[0] if ts else None,
        "last_ledger_t": ts[-1] if ts else None,
        "ledgers": len(led),
        "ledger_range": [observed[0], observed[-1]] if observed else None,
        "ct_kinds": dict(kinds),
        "validations": len(vals),
        "duplicates_dropped": d["dups"],
        "unmatched_validations": unmatched,
        "non_validated_validations": sum(nonval.values()),
        "partial_validations": partial,
        "validators": len(rows),
        "network_d_first": dist(all_first),
        "network_skew": dist(all_skew),
        "network_late_share": round(sum(1 for x in all_first if x > late) / len(all_first), 4)
        if all_first
        else None,
        "hourly": [
            {
                "hour": h,
                **dist(x),
                "late_share": round(sum(1 for y in x if y > late) / len(x), 4),
            }
            for h, x in sorted(hourly.items())
        ],
        "gaps": d["gaps"],
        "sessions": len(d["sessions"]),
        "per_validator": rows,
    }
    s["window"] = [
        datetime.fromtimestamp(ts[0], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if ts else None,
        datetime.fromtimestamp(ts[-1], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if ts else None,
    ]
    return s


def f(x, nd=2):
    return "-" if x is None else f"{x:.{nd}f}"


def pct(x):
    return "-" if x is None else f"{100 * x:.2f}%"


def render(s, title="vlatency report"):
    L = [
        f"# {title}",
        "",
        f"Window (validated-ledger receive times, UTC): {s['window'][0]} .. {s['window'][1]}; "
        f"ledgers {s['ledgers']}"
        + (f" ({s['ledger_range'][0]}-{s['ledger_range'][1]})" if s["ledger_range"] else "")
        + f"; validations {s['validations']} (duplicates dropped {s['duplicates_dropped']});"
        f" validators (signing keys) {s['validators']}; collector sessions {s['sessions']}.",
        "",
        "Single vantage point: every latency below is when THIS node received the message. It mixes the"
        " validator's own speed with the relay path to this node. See the README for the limits.",
        "",
        "## Network",
        "",
        "| metric | n | p50 | p90 | p99 | max |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name, key in (("d_first (s behind the first validation)", "network_d_first"),
                      ("signing skew (s, st - receive)", "network_skew")):
        x = s[key]
        if x.get("n"):
            L.append(f"| {name} | {x['n']} | {f(x['p50'])} | {f(x['p90'])} | {f(x['p99'])} | {f(x['max'])} |")
    L += [
        "",
        f"- late share (d_first > {s['late_threshold_s']} s): {pct(s['network_late_share'])}",
        f"- validations for a non-validated hash: {s['non_validated_validations']};"
        f" partial (full=false): {s['partial_validations']};"
        f" for a ledger not seen validated here: {s['unmatched_validations']}",
        f"- close_time kinds (resolution {s['close_time_resolution_s']} s): "
        + ", ".join(f"{k} {v}" for k, v in sorted(s["ct_kinds"].items()))
        + ". d_ct uses rounded ledgers only.",
        "",
        "## Per validator",
        "",
        "Sorted by d_first p50. Seconds. `skew-net` = this validator's median skew minus the network median.",
        "",
        "| signing key | matched | span cov | d_first p50 | p90 | p99 | max | d_closed p50 | d_ct p50 |"
        " skew p50 | skew-net | late | non-val |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in s["per_validator"]:
        a = r["d_first"]
        L.append(
            f"| `{r['key']}` | {r['matched']} | {pct(r['span_coverage'])} | {f(a.get('p50'))} |"
            f" {f(a.get('p90'))} | {f(a.get('p99'))} | {f(a.get('max'))} | {f(r['d_closed'].get('p50'))} |"
            f" {f(r['d_ct'].get('p50'))} | {f(r['skew'].get('p50'))} | {f(r['skew_vs_network'])} |"
            f" {pct(r['late_share'])} | {r['non_validated']} |"
        )
    L += ["", "## Hourly (network d_first)", "", "| hour (UTC) | n | p50 | p90 | p99 | max | late |",
          "|---|---:|---:|---:|---:|---:|---:|"]
    for h in s["hourly"]:
        L.append(
            f"| {h['hour']} | {h['n']} | {f(h['p50'])} | {f(h['p90'])} | {f(h['p99'])} | {f(h['max'])} |"
            f" {pct(h['late_share'])} |"
        )
    if s["gaps"]:
        L += ["", "## Gaps", "", "| from | to | count | reason |", "|---:|---:|---:|---|"]
        L += [f"| {g['from']} | {g['to']} | {g['count']} | {g['reason']} |" for g in s["gaps"]]
    return "\n".join(L) + "\n"


def write_csv(s, path):
    cols = ["key", "master_key", "matched", "first_ledger", "last_ledger", "span_coverage", "window_coverage",
            "late_share", "non_validated", "partial", "skew_vs_network"]
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols + [f"{m}_{q}" for m in ("d_first", "d_closed", "d_ct", "skew")
                           for q in ("p50", "p90", "p99", "max")])
        for r in s["per_validator"]:
            w.writerow([r[c] for c in cols] + [r[m].get(q) for m in ("d_first", "d_closed", "d_ct", "skew")
                                               for q in ("p50", "p90", "p99", "max")])


SURFACE, INK, MUTED, GRID, SERIES = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df", "#2a78d6"


def _axes(ax, grid="y"):
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(MUTED)
    ax.tick_params(colors=MUTED, labelsize=9)
    ax.grid(axis=grid, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def charts(evs, s, outdir):
    """per_validator.png (p50..p90 ranges), skew.png (median skew per validator), network.png (histogram)."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    rows = [r for r in s["per_validator"] if r["d_first"].get("n")]
    files = []
    if rows:
        fig, ax = plt.subplots(figsize=(8, 4.2), facecolor=SURFACE)
        _axes(ax)
        xs = range(len(rows))
        p50 = [r["d_first"]["p50"] for r in rows]
        p90 = [r["d_first"]["p90"] for r in rows]
        ax.vlines(xs, p50, p90, color=SERIES, linewidth=2)
        ax.scatter(xs, p50, s=36, color=SERIES, edgecolors=SURFACE, linewidths=2, zorder=3)
        ax.set_xticks([])
        ax.set_xlabel(f"validators (signing keys), sorted by p50; n={len(rows)}", color=MUTED, fontsize=9)
        ax.set_ylabel("seconds behind the first validation", color=MUTED, fontsize=9)
        ax.set_ylim(bottom=0)
        ax.set_title("Validation receive delay per validator (dot = p50, line up to p90)",
                     color=INK, fontsize=11, loc="left")
        fig.tight_layout()
        fig.savefig(os.path.join(outdir, "per_validator.png"), dpi=110)
        plt.close(fig)
        files.append("per_validator.png")
        sk = sorted((r["skew"]["p50"], r["key"]) for r in rows if r["skew"].get("n"))
        fig, ax = plt.subplots(figsize=(8, 3.8), facecolor=SURFACE)
        _axes(ax)
        ax.axhline(0, color=MUTED, linewidth=1)
        ax.scatter(range(len(sk)), [x for x, _ in sk], s=36, color=SERIES, edgecolors=SURFACE,
                   linewidths=2, zorder=3)
        ax.set_xticks([])
        ax.set_xlabel("validators (signing keys), sorted by median skew", color=MUTED, fontsize=9)
        ax.set_ylabel("signing_time - receive time (s)", color=MUTED, fontsize=9)
        ns = s["network_skew"]
        ax.set_title(f"Median signing-clock skew per validator (network p50 {ns['p50']:.2f} s;"
                     " whole-second signing_time)", color=INK, fontsize=11, loc="left")
        fig.tight_layout()
        fig.savefig(os.path.join(outdir, "skew.png"), dpi=110)
        plt.close(fig)
        files.append("skew.png")
        d = build(evs, s["close_time_resolution_s"])
        led = d["ledgers"]
        first, dd = {}, []
        for v in d["vals"]:
            L = led.get(v["li"])
            if L and L.get("lh") == v["lh"]:
                first[v["li"]] = min(first.get(v["li"], v["t"]), v["t"])
        for v in d["vals"]:
            if v["li"] in first and led[v["li"]]["lh"] == v["lh"]:
                dd.append(v["t"] - first[v["li"]])
        hi = percentile(dd, 99.5)
        fig, ax = plt.subplots(figsize=(8, 3.6), facecolor=SURFACE)
        _axes(ax)
        ax.hist([min(x, hi) for x in dd], bins=40, color=SERIES, edgecolor=SURFACE, linewidth=2)
        ax.axvline(s["late_threshold_s"], color=MUTED, linewidth=1, linestyle="--")
        ax.text(s["late_threshold_s"], ax.get_ylim()[1] * 0.92, f"  late > {s['late_threshold_s']} s",
                color=MUTED, fontsize=9)
        ax.set_xlabel(
            "seconds behind the first validation of the same ledger (values above p99.5 in last bin)",
            color=MUTED,
            fontsize=9,
        )
        ax.set_ylabel("validations", color=MUTED, fontsize=9)
        nd = s["network_d_first"]
        ax.set_title(f"All validations (n={nd['n']}, p50 {nd['p50']:.2f} s, p99 {nd['p99']:.2f} s)",
                     color=INK, fontsize=11, loc="left")
        fig.tight_layout()
        fig.savefig(os.path.join(outdir, "network.png"), dpi=110)
        plt.close(fig)
        files.append("network.png")
    return files


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("vl")
    ap.add_argument("--report", required=True)
    ap.add_argument("--late", type=float, default=2.0)
    ap.add_argument("--resolution", type=int, default=10, help="close_time_resolution in s (ledger RPC)")
    ap.add_argument("--title", default="vlatency report")
    ap.add_argument("--charts", action="store_true")
    a = ap.parse_args(argv)
    evs, bad = load(a.vl)
    s = analyze(evs, a.late, a.resolution)
    s["bad_lines"] = bad
    os.makedirs(a.report, exist_ok=True)
    with open(os.path.join(a.report, "summary.json"), "w") as fh:
        json.dump(s, fh, indent=1, sort_keys=True)
    write_csv(s, os.path.join(a.report, "validators.csv"))
    md = render(s, a.title)
    if a.charts:
        md += "\n## Charts\n\n" + "".join(f"![{x}]({x})\n\n" for x in charts(evs, s, a.report))
    with open(os.path.join(a.report, "REPORT.md"), "w") as fh:
        fh.write(md)
    print(render(s, a.title)[:3000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
