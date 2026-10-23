#!/usr/bin/env python3
"""Per-validator participation analysis of a collect_validations.py window (stdlib only).

Reads the CSV/JSONL files written by collect_validations.py and reports, for
every validator master key seen (and every key on the node's trusted list):

  participation %   canonical validations / ledgers observed in the window
  missed, streak    ledgers with no canonical validation; longest run of them
  non-canonical     validations whose ledger_hash differs from the validated ledger
  sign delay        median signing_time - ledger close time (s, 1 s resolution)
  recv lag          median recv_utc - signing_time (s), i.e. relay + clock skew
  first/last seen   for churn (validators joining or leaving during the window)

Writes REPORT.md, validators_summary.csv, ledger_quorum.csv and three PNG charts
(matplotlib, only needed for charts; --no-charts skips them).

and, per ledger, how many trusted validators signed the canonical hash
compared with the node's validation_quorum.

"Ledgers observed" are those in ledgers.csv (the node's validated ledger
stream). Ledgers listed in gaps.csv were not observed and are excluded from
every denominator. The node only sees validations relayed to it, so a low
figure can mean poor connectivity between that validator and this node, not
necessarily a validator fault.

Usage:
  ./analyze_validations.py --data data/validators --out data/validators/report
"""

import argparse
import csv
import json
import os
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone

RIPPLE_EPOCH = 946684800
CHURN_MARGIN = 100     # ledgers (~5 min): first/last seen this far inside the window counts as churn
SERIES = ["#2a78d6", "#eb6834"]   # UNL, non-UNL (same palette as txmix/)
OTHER = "#8c8c8c"
INK = "#3c3c3a"

SUMMARY_FIELDS = [
    "master_key", "domain", "trusted", "observed_ledgers", "canonical", "participation_pct",
    "active_ledgers", "active_participation_pct", "missed", "longest_missed_streak", "non_canonical",
    "median_sign_delay_s", "median_recv_lag_s", "first_ledger", "last_ledger",
]
LEDGER_FIELDS = ["ledger_index", "trusted_canonical", "quorum", "below_quorum", "all_canonical"]


def parse_iso(s):
    return datetime.strptime(s.rstrip("Z"), "%Y-%m-%dT%H:%M:%S.%f").replace(tzinfo=timezone.utc).timestamp()


def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def load_snapshots(path):
    """Return (trusted master keys, quorum) from the last `validators` snapshot."""
    trusted, quorum = set(), None
    if not os.path.exists(path):
        return trusted, quorum
    with open(path) as f:
        for line in f:
            d = json.loads(line)
            r = d.get("result", {})
            if d.get("command") == "validators":
                trusted = set(r.get("trusted_validator_keys", []))
                quorum = r.get("validation_quorum", quorum)
            elif d.get("command") == "server_info" and quorum is None:
                quorum = r.get("validation_quorum")
    return trusted, quorum


def unl_changes(path):
    """Return (first, last) trusted key sets over all `validators` snapshots in the window."""
    sets = []
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                d = json.loads(line)
                if d.get("command") == "validators":
                    sets.append(set(d.get("result", {}).get("trusted_validator_keys", [])))
    if not sets:
        return set(), set()
    return sets[0], sets[-1]


def churn(rows, first_idx, last_idx, margin=CHURN_MARGIN):
    """Validators first seen > margin ledgers after the window start (joined) or last seen
    > margin before its end (left). Keys never seen are reported separately as silent."""
    joined, left, silent = [], [], []
    for r in rows:
        if r["first_ledger"] == "":
            silent.append(r["master_key"])
            continue
        if r["first_ledger"] - first_idx > margin:
            joined.append(r["master_key"])
        if last_idx - r["last_ledger"] > margin:
            left.append(r["master_key"])
    return joined, left, silent


def unl_split(rows):
    """Aggregate participation for UNL (trusted=1) and non-UNL validators."""
    out = {}
    for flag, name in ((1, "unl"), (0, "non_unl")):
        pcts = [r["participation_pct"] for r in rows if r["trusted"] == flag]
        out[name] = {
            "validators": len(pcts),
            "median_participation_pct": round(statistics.median(pcts), 2) if pcts else "",
            "min_participation_pct": min(pcts) if pcts else "",
            "canonical_validations": sum(r["canonical"] for r in rows if r["trusted"] == flag),
        }
    return out


def longest_streak(observed, signed):
    """Longest run of consecutive observed ledgers (sorted list) not in `signed`."""
    best = cur = 0
    for idx in observed:
        if idx in signed:
            cur = 0
        else:
            cur += 1
            best = max(best, cur)
    return best


def analyze(ledgers, validations, trusted=frozenset(), quorum=None, domains=None):
    """Core computation; inputs are lists of dicts as read from the CSVs."""
    domains = domains or {}
    canonical = {int(r["ledger_index"]): r["ledger_hash"] for r in ledgers}
    close_time = {int(r["ledger_index"]): int(r["ledger_time"]) for r in ledgers}
    observed = sorted(canonical)
    obs_set = set(observed)

    signed = defaultdict(set)        # master -> ledger indexes with a canonical validation
    noncanon = defaultdict(set)      # master -> ledger indexes with only/also a different hash
    delays = defaultdict(list)
    lags = defaultdict(list)
    seen = defaultdict(lambda: [None, None])
    for v in validations:
        idx = int(v["ledger_index"])
        if idx not in obs_set:
            continue
        m = v["master_key"]
        s = seen[m]
        s[0] = idx if s[0] is None else min(s[0], idx)
        s[1] = idx if s[1] is None else max(s[1], idx)
        if v["ledger_hash"] == canonical[idx]:
            if idx not in signed[m]:
                signed[m].add(idx)
                st = int(v["signing_time"])
                delays[m].append(st - close_time[idx])
                lags[m].append(parse_iso(v["recv_utc"]) - (st + RIPPLE_EPOCH))
        else:
            noncanon[m].add(idx)

    rows = []
    for m in sorted(set(seen) | set(trusted)):
        n = len(observed)
        c = len(signed[m])
        lo, hi = seen[m]
        act = sum(1 for i in observed if lo <= i <= hi) if lo is not None else 0
        rows.append({
            "master_key": m,
            "domain": domains.get(m, ""),
            "trusted": int(m in trusted),
            "observed_ledgers": n,
            "canonical": c,
            "participation_pct": round(100.0 * c / n, 2) if n else 0.0,
            "active_ledgers": act,
            "active_participation_pct": round(100.0 * c / act, 2) if act else "",
            "missed": n - c,
            "longest_missed_streak": longest_streak(observed, signed[m]),
            "non_canonical": len(noncanon[m] - signed[m]),
            "median_sign_delay_s": float(statistics.median(delays[m])) if delays[m] else "",
            "median_recv_lag_s": round(statistics.median(lags[m]), 3) if lags[m] else "",
            "first_ledger": seen[m][0] if seen[m][0] is not None else "",
            "last_ledger": seen[m][1] if seen[m][1] is not None else "",
        })
    rows.sort(key=lambda r: (-r["trusted"], r["participation_pct"], r["master_key"]))

    per_ledger = []
    for idx in observed:
        tc = sum(1 for m in trusted if idx in signed[m])
        per_ledger.append({
            "ledger_index": idx,
            "trusted_canonical": tc,
            "quorum": quorum if quorum is not None else "",
            "below_quorum": int(quorum is not None and tc < quorum),
            "all_canonical": sum(1 for m in signed if idx in signed[m]),
        })
    return rows, per_ledger


def write_csv(path, fields, rows):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def label(r):
    """Public identity only: manifest domain + key prefix (domains repeat), else the short master key."""
    return f"{r['domain']} ({r['master_key'][:6]})" if r["domain"] else r["master_key"][:12] + "…"


def _style(ax, plt_title, xlabel, ylabel):
    ax.set_title(plt_title, color=INK)
    ax.set_xlabel(xlabel, color=INK)
    ax.set_ylabel(ylabel, color=INK)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.set_axisbelow(True)


def chart_participation(rows, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Patch

    # Missed ledgers rather than %: near-100% participation is unreadable on a 0-100 axis.
    rs = sorted(rows, key=lambda r: (r["missed"], r["master_key"]))
    fig, ax = plt.subplots(figsize=(9, max(3.0, 0.22 * len(rs) + 1)))
    ax.barh(range(len(rs)), [r["missed"] for r in rs], height=0.75,
            color=[SERIES[0] if r["trusted"] else SERIES[1] for r in rs])
    ax.set_yticks(range(len(rs)))
    ax.set_yticklabels([f"{label(r)} {r['participation_pct']}%" for r in rs], fontsize=7, family="monospace")
    ax.set_ylim(-0.6, len(rs) - 0.4)
    ax.grid(axis="x", color="#e4e4e0", linewidth=0.6)
    n = rs[0]["observed_ledgers"] if rs else 0
    _style(ax, f"Missed ledgers per validator (of {n} observed; label shows participation %)",
           "ledgers without a canonical validation received", "")
    handles = [Patch(color=SERIES[0], label="on node UNL"), Patch(color=SERIES[1], label="not on UNL")]
    ax.legend(handles=handles, loc="upper left", bbox_to_anchor=(1.0, 1.0), frameon=False)
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


def chart_quorum(per_ledger, quorum, path, bucket=100):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    xs, mins, means = [], [], []
    for i in range(0, len(per_ledger), bucket):
        b = per_ledger[i:i + bucket]
        xs.append(b[0]["ledger_index"])
        vals = [r["trusted_canonical"] for r in b]
        mins.append(min(vals))
        means.append(sum(vals) / len(vals))
    fig, ax = plt.subplots(figsize=(10, 4))
    ax.plot(xs, means, color=SERIES[0], linewidth=1.8, zorder=3, label=f"mean per {bucket} ledgers")
    ax.plot(xs, mins, color=SERIES[1], linewidth=1.2, label=f"minimum per {bucket} ledgers")
    if quorum is not None:
        ax.axhline(quorum, color=OTHER, linestyle="--", linewidth=1.2, label=f"validation_quorum = {quorum}")
    ax.set_ylim(bottom=0)
    ax.grid(axis="y", color="#e4e4e0", linewidth=0.6)
    ax.ticklabel_format(axis="x", style="plain", useOffset=False)
    _style(ax, "Trusted validators signing the canonical ledger vs quorum", "ledger index",
           "trusted canonical validations")
    ax.legend(loc="lower left", frameon=False)
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


def chart_recv_lag(rows, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    groups = []
    for flag in (1, 0):
        groups.append([r["median_recv_lag_s"] for r in rows
                       if r["trusted"] == flag and r["median_recv_lag_s"] != ""])
    fig, ax = plt.subplots(figsize=(8, 4))
    hi = max([v for g in groups for v in g] + [1.0])
    bins = [hi * i / 30 for i in range(31)]
    ax.hist(groups, bins=bins, stacked=True, color=SERIES, label=["on node UNL", "not on UNL"],
            edgecolor="white", linewidth=0.8)
    ax.grid(axis="y", color="#e4e4e0", linewidth=0.6)
    _style(ax, "Median receive lag per validator (recv_utc − signing_time)", "seconds", "validators")
    ax.legend(loc="upper right", frameon=False)
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


def render(rows, per_ledger, window, gaps, quorum, trusted, unl, joined, left, silent, n_validations,
           charts=True):
    n = len(per_ledger)
    first = per_ledger[0]["ledger_index"] if n else "?"
    last = per_ledger[-1]["ledger_index"] if n else "?"
    by = {r["master_key"]: r for r in rows}
    split = unl_split(rows)
    out = []
    w = out.append
    w("# Per-validator participation — Post Fiat testnet\n")
    w(f"Window: **{window.get('start_utc', '?')} → {window.get('end_utc', '?')}** (UTC), one node's view "
      "(validations relayed to it), testnet only.  ")
    w(f"Ledgers observed: **{n}** ({first}–{last}); gap ledgers excluded: "
      f"**{sum(int(g['count']) for g in gaps)}** in {len(gaps)} gap(s).  ")
    w(f"Validation rows: {n_validations}; validators seen or trusted: {len(rows)}.  ")
    w(f"Node UNL: {len(trusted)} trusted keys, validation_quorum {quorum}.\n")
    w("Generated by `validators/analyze_validations.py` from data collected by "
      "`validators/collect_validations.py`. Identities are public master keys and manifest domains only.\n")

    w("## UNL vs non-UNL\n")
    w("| group | validators | median participation % | lowest % | canonical validations |")
    w("|---|---:|---:|---:|---:|")
    for name, title in (("unl", "on node UNL"), ("non_unl", "not on UNL")):
        g = split[name]
        w(f"| {title} | {g['validators']} | {g['median_participation_pct']} | {g['min_participation_pct']} "
          f"| {g['canonical_validations']} |")
    w("")

    below = [r["ledger_index"] for r in per_ledger if r["below_quorum"]]
    tc = [r["trusted_canonical"] for r in per_ledger]
    w("## Validators per ledger vs quorum\n")
    if tc:
        w(f"Trusted canonical validations per ledger: min {min(tc)}, median {statistics.median(tc)}, "
          f"max {max(tc)} (quorum {quorum}).  ")
    w(f"Ledgers below quorum in this node's view: **{len(below)}**"
      + (f" (first: {', '.join(str(x) for x in below[:10])})" if below else "") + ".\n")
    w("A ledger \"below quorum\" here means this node *received* fewer trusted validations for the canonical "
      "hash than the quorum; the network still validated it (it is in the node's validated stream).\n")

    w(f"## Churn (first/last seen more than {CHURN_MARGIN} ledgers inside the window)\n")
    w(f"- Joined during window: {len(joined)}"
      + "".join(f"\n  - `{label(by[m])}` first {by[m]['first_ledger']}" for m in joined))
    w(f"- Left during window: {len(left)}"
      + "".join(f"\n  - `{label(by[m])}` last {by[m]['last_ledger']}" for m in left))
    w(f"- Trusted but never seen: {len(silent)}" + "".join(f"\n  - `{m}`" for m in silent))
    w(f"- UNL changes between first and last `validators` snapshot: added {len(unl['added'])}, "
      f"removed {len(unl['removed'])}\n")

    if charts:
        w("## Charts\n")
        w("![participation](participation.png)\n")
        w("![quorum](quorum.png)\n")
        w("![receive lag](recv_lag.png)\n")

    w("## Per-validator table\n")
    w("Sign delay = median `signing_time` − ledger close time (1 s resolution). Receive lag = median arrival "
      "at this node − `signing_time` (relay latency plus clock skew).\n")
    w("Participation % uses every observed ledger in the window; active % uses only observed ledgers "
      "between the validator's first and last validation seen, so a validator that joined or left mid-window "
      "is not penalised there for ledgers before or after it existed (see Churn).\n")
    w("| validator | UNL | participation % | active % | missed | longest streak | non-canonical "
      "| sign delay s | recv lag s | first | last |")
    w("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in rows:
        ident = f"{r['domain']} `{r['master_key']}`" if r["domain"] else f"`{r['master_key']}`"
        w(f"| {ident} | {'yes' if r['trusted'] else 'no'} | {r['participation_pct']} "
          f"| {r['active_participation_pct']} | {r['missed']} "
          f"| {r['longest_missed_streak']} | {r['non_canonical']} | {r['median_sign_delay_s']} "
          f"| {r['median_recv_lag_s']} | {r['first_ledger']} | {r['last_ledger']} |")
    w("\n## Limits\n")
    w("- One node's view: a missed ledger means no validation for the canonical hash *reached this node*; "
      "it can be the validator, its peering, or this node's connectivity.")
    w("- The node keeps only recent history (`online_delete`), so the run cross-checks ledgers against the "
      "`ledger` RPC right after the window rather than later.")
    w("- Sign delay has 1 s resolution (ledger close and signing times are whole seconds).")
    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data", default="data/validators")
    ap.add_argument("--out", default=None, help="output dir (default: <data>/report)")
    ap.add_argument("--no-charts", action="store_true", help="skip PNG charts (no matplotlib needed)")
    a = ap.parse_args()
    out = a.out or os.path.join(a.data, "report")
    os.makedirs(out, exist_ok=True)

    ledgers = read_csv(os.path.join(a.data, "ledgers.csv"))
    validations = read_csv(os.path.join(a.data, "validations.csv"))
    gaps = read_csv(os.path.join(a.data, "gaps.csv"))
    domains = {}
    for r in read_csv(os.path.join(a.data, "manifests.csv")):
        if r.get("master_key"):
            domains[r["master_key"]] = r.get("domain", "")
    trusted, quorum = load_snapshots(os.path.join(a.data, "snapshots.jsonl"))
    first_unl, last_unl = unl_changes(os.path.join(a.data, "snapshots.jsonl"))
    window = {}
    wpath = os.path.join(a.data, "window.json")
    if os.path.exists(wpath):
        with open(wpath) as f:
            window = json.load(f)
    if not ledgers:
        sys.exit("no ledgers.csv rows")

    rows, per_ledger = analyze(ledgers, validations, trusted, quorum, domains)
    write_csv(os.path.join(out, "validators_summary.csv"), SUMMARY_FIELDS, rows)
    write_csv(os.path.join(out, "ledger_quorum.csv"), LEDGER_FIELDS, per_ledger)
    joined, left, silent = churn(rows, per_ledger[0]["ledger_index"], per_ledger[-1]["ledger_index"])
    unl = {"added": sorted(last_unl - first_unl), "removed": sorted(first_unl - last_unl)}
    if not a.no_charts:
        chart_participation(rows, os.path.join(out, "participation.png"))
        chart_quorum(per_ledger, quorum, os.path.join(out, "quorum.png"))
        chart_recv_lag(rows, os.path.join(out, "recv_lag.png"))
    md = render(rows, per_ledger, window, gaps, quorum, trusted, unl, joined, left, silent, len(validations),
                charts=not a.no_charts)
    with open(os.path.join(out, "REPORT.md"), "w") as f:
        f.write(md)
    print(f"validations={len(validations)} ledgers={len(ledgers)} gaps={len(gaps)} "
          f"validators={len(rows)} trusted={len(trusted)} quorum={quorum}")
    print(f"churn: joined={len(joined)} left={len(left)} silent={len(silent)}; "
          f"unl added={len(unl['added'])} removed={len(unl['removed'])}; "
          f"below_quorum={sum(r['below_quorum'] for r in per_ledger)}")


if __name__ == "__main__":
    main()
