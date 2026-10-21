"""Pure statistics helpers for pft-ledger-stats (no I/O, unit-tested)."""

import csv
import json
from collections import OrderedDict

DROPS_PER_PFT = 1_000_000


def load_ledgers(path):
    """Read ledgers.csv into a list of dicts with int fields, sorted by ledger_index."""
    rows = []
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            rows.append({
                "ledger_index": int(r["ledger_index"]),
                "ledger_hash": r["ledger_hash"],
                "close_time": int(r["close_time"]),
                "close_time_iso": r["close_time_iso"],
                "close_time_resolution": int(r["close_time_resolution"]),
                "txn_count": int(r["txn_count"]),
                "total_coins": int(r["total_coins"]),
            })
    rows.sort(key=lambda r: r["ledger_index"])
    return rows


def load_samples(path):
    with open(path) as fh:
        return [json.loads(line) for line in fh if line.strip()]


def percentile(values, p):
    """Linear-interpolated percentile (same as numpy's default), p in [0, 100]."""
    if not values:
        raise ValueError("percentile of empty sequence")
    if not 0 <= p <= 100:
        raise ValueError("p must be within [0, 100]")
    s = sorted(values)
    k = (len(s) - 1) * p / 100.0
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def summarize(values):
    return {
        "count": len(values),
        "min": min(values),
        "median": percentile(values, 50),
        "p95": percentile(values, 95),
        "p99": percentile(values, 99),
        "max": max(values),
        "mean": sum(values) / len(values),
    }


def find_gaps(rows):
    """Return a list of (prev_index, next_index) where ledger indices are not consecutive."""
    return [(a["ledger_index"], b["ledger_index"]) for a, b in zip(rows, rows[1:], strict=False)
            if b["ledger_index"] != a["ledger_index"] + 1]


def close_intervals(rows):
    """Seconds between consecutive ledgers' close_time; pairs across a gap are skipped.

    Each item: (ledger_index, close_time_iso, interval_s) for the later ledger.
    """
    out = []
    for a, b in zip(rows, rows[1:], strict=False):
        if b["ledger_index"] == a["ledger_index"] + 1:
            out.append((b["ledger_index"], b["close_time_iso"], b["close_time"] - a["close_time"]))
    return out


def stalls(intervals, factor=3.0, floor=0):
    """Intervals strictly greater than factor * max(median interval, floor).

    floor=0 is the literal "3x median" rule. Passing floor=close_time_resolution gives a
    resolution-aware rule: close_time is rounded, so raw deltas below the resolution are noise.
    """
    vals = [i[2] for i in intervals]
    if not vals:
        return 0, []
    limit = factor * max(percentile(vals, 50), floor)
    return limit, [i for i in intervals if i[2] > limit]


def by_hour(rows):
    """OrderedDict 'YYYY-MM-DDTHH' -> {ledgers, txns, burn_drops} (burn within that hour)."""
    out = OrderedDict()
    prev = None
    for r in rows:
        h = r["close_time_iso"][:13]
        b = out.setdefault(h, {"ledgers": 0, "txns": 0, "burn_drops": 0})
        b["ledgers"] += 1
        b["txns"] += r["txn_count"]
        if prev is not None and r["ledger_index"] == prev["ledger_index"] + 1:
            b["burn_drops"] += prev["total_coins"] - r["total_coins"]
        prev = r
    return out


def fee_burn(rows):
    """Total drops burned between first and last ledger, and per-ledger burn values."""
    per = [a["total_coins"] - b["total_coins"] for a, b in zip(rows, rows[1:], strict=False)
           if b["ledger_index"] == a["ledger_index"] + 1]
    total = rows[0]["total_coins"] - rows[-1]["total_coins"] if rows else 0
    return total, per


def quorum_margin(samples):
    """Per sample: proposers - validation_quorum (negative means below quorum)."""
    return [(s["sampled_at"], s["proposers"], s["validation_quorum"], s["proposers"] - s["validation_quorum"])
            for s in samples if s.get("proposers") is not None and s.get("validation_quorum") is not None]
