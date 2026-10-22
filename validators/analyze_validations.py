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

SUMMARY_FIELDS = [
    "master_key", "domain", "trusted", "observed_ledgers", "canonical", "participation_pct",
    "missed", "longest_missed_streak", "non_canonical", "median_sign_delay_s", "median_recv_lag_s",
    "first_ledger", "last_ledger",
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
        rows.append({
            "master_key": m,
            "domain": domains.get(m, ""),
            "trusted": int(m in trusted),
            "observed_ledgers": n,
            "canonical": c,
            "participation_pct": round(100.0 * c / n, 2) if n else 0.0,
            "missed": n - c,
            "longest_missed_streak": longest_streak(observed, signed[m]),
            "non_canonical": len(noncanon[m] - signed[m]),
            "median_sign_delay_s": statistics.median(delays[m]) if delays[m] else "",
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


def markdown(rows, per_ledger, window, gaps, quorum, trusted):
    n = len(per_ledger)
    lines = [
        "# Per-validator participation",
        "",
        f"- Window: {window.get('start_utc', '?')} -> {window.get('end_utc', '?')}",
        f"- Ledgers observed: {n}"
        + (f" ({per_ledger[0]['ledger_index']}-{per_ledger[-1]['ledger_index']})" if n else ""),
        f"- Gap ledgers (excluded): {sum(int(g['count']) for g in gaps)} in {len(gaps)} gap(s)",
        f"- Trusted validators (node UNL): {len(trusted)}; validation_quorum: {quorum}",
        f"- Ledgers with fewer trusted canonical validations than quorum: "
        f"{sum(r['below_quorum'] for r in per_ledger)}",
        "",
        "| master key | domain | UNL | participation % | missed | longest streak | non-canonical "
        "| sign delay s | recv lag s |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for r in rows:
        lines.append(
            f"| `{r['master_key']}` | {r['domain']} | {'yes' if r['trusted'] else 'no'} "
            f"| {r['participation_pct']} | {r['missed']} | {r['longest_missed_streak']} "
            f"| {r['non_canonical']} | {r['median_sign_delay_s']} | {r['median_recv_lag_s']} |"
        )
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data", default="data/validators")
    ap.add_argument("--out", default=None, help="output dir (default: <data>/report)")
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
    md = markdown(rows, per_ledger, window, gaps, quorum, trusted)
    with open(os.path.join(out, "SUMMARY.md"), "w") as f:
        f.write(md)
    print(f"validations={len(validations)} ledgers={len(ledgers)} gaps={len(gaps)} "
          f"validators={len(rows)} trusted={len(trusted)} quorum={quorum}")
    print(md)


if __name__ == "__main__":
    main()
