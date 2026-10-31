#!/usr/bin/env python3
"""Sender activity cohorts, retention and regularity from a senders/ collection (stdlib only).

Reads tx.csv / ledgers.csv / gaps.csv written by collect_senders.py and prints (and with --json
writes) one deterministic result:

  days        window split into 24 h slices from the first ledger's close time (slice 0, 1, 2 ...);
              the slice count is the window length in days rounded to the nearest integer and the
              last slice absorbs the remainder (72.5 h -> 3 slices, the last one 24.5 h long);
              per slice: active senders, new senders (first seen in the window in this slice) and
              returning senders (seen in an earlier slice). "New" is window-relative: an account
              that last sent before the window started counts as new in slice 0.
  retention   cohort = slice of first appearance; cell [c][k] = senders of cohort c active in slice k
  regularity  per sender, over the close times of the ledgers it sent in (one event per ledger):
              n events, inter-arrival median/mean/CV, burstiness B=(sd-mean)/(sd+mean), and
              period_share = share of gaps within +-PERIOD_TOL of the median gap
  classes     periodic / bursty / sporadic by the rule in classify(); thresholds are fixed constants
              checked against synthetic periodic, Poisson and burst controls (controls_senders.py)
  shares      per class: senders, transactions, fees (drops), failed (result != tesSUCCESS)
  top         most active periodic senders, addresses truncated (first 6 + last 4 characters)

Exit 1 if the ledger range has a hole that is not listed in gaps.csv (incomplete collection).
"""

import argparse
import csv
import json
import math
import os
import sys
from collections import Counter, defaultdict

DAY = 86400
MIN_EVENTS = 6          # fewer events -> "sporadic" (too few gaps to judge regularity)
PERIODIC_CV = 0.5       # periodic: CV of gaps <= 0.5 ...
PERIOD_TOL = 0.2        # ... and >= PERIOD_SHARE of gaps within +-20 % of the median gap
PERIOD_SHARE = 0.6
BURSTY_B = 0.25         # bursty: burstiness >= 0.25 (CV >= 1.67); Poisson has B ~ 0, CV ~ 1
CLASSES = ("periodic", "bursty", "sporadic")


def gap_stats(times):
    """Regularity statistics of a sorted list of distinct event times (seconds)."""
    gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
    n = len(times)
    if not gaps:
        return {"n": n, "gaps": 0, "median_gap": None, "mean_gap": None, "cv": None, "b": None,
                "period_share": None}
    s = sorted(gaps)
    m = len(s)
    med = s[m // 2] if m % 2 else (s[m // 2 - 1] + s[m // 2]) / 2
    mean = sum(gaps) / m
    sd = math.sqrt(sum((g - mean) ** 2 for g in gaps) / m)
    cv = sd / mean if mean > 0 else None
    b = (sd - mean) / (sd + mean) if sd + mean > 0 else None
    share = sum(1 for g in gaps if abs(g - med) <= PERIOD_TOL * med) / m if med > 0 else 0.0
    return {"n": n, "gaps": m, "median_gap": med, "mean_gap": mean, "cv": cv, "b": b,
            "period_share": share}


def classify(st):
    if st["n"] < MIN_EVENTS or st["cv"] is None:
        return "sporadic"
    if st["cv"] <= PERIODIC_CV and st["period_share"] >= PERIOD_SHARE:
        return "periodic"
    if st["b"] is not None and st["b"] >= BURSTY_B:
        return "bursty"
    return "sporadic"


def truncate(addr):
    return addr if len(addr) <= 12 else f"{addr[:6]}...{addr[-4:]}"


def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def coverage(ledgers, gaps):
    idx = sorted(int(r["ledger_index"]) for r in ledgers)
    gap_idx = sorted(int(r["ledger_index"]) for r in gaps)
    if not idx:
        return {"first": None, "last": None, "fetched": 0, "gaps": len(gap_idx), "holes": []}
    have = set(idx) | set(gap_idx)
    holes = [i for i in range(idx[0], idx[-1] + 1) if i not in have]
    dup = len(idx) - len(set(idx))
    return {"first": idx[0], "last": idx[-1], "fetched": len(idx), "gaps": len(gap_idx),
            "gap_ledgers": gap_idx, "holes": holes, "duplicates": dup}


def analyze(txs, ledgers, gaps, top_n=10):
    cov = coverage(ledgers, gaps)
    t0 = min(int(r["close_time"]) for r in ledgers) if ledgers else 0
    t1 = max(int(r["close_time"]) for r in ledgers) if ledgers else 0
    nslices = max(1, round((t1 - t0) / DAY))

    def slice_of(ct):
        return min((int(ct) - t0) // DAY, nslices - 1)

    by_sender = defaultdict(list)
    for r in txs:
        by_sender[r["account"]].append(r)

    first_slice = {}
    active = defaultdict(set)
    for acct, rows in by_sender.items():
        sl = sorted({slice_of(r["close_time"]) for r in rows})
        first_slice[acct] = sl[0]
        for s in sl:
            active[s].add(acct)
    days = []
    for s in range(nslices):
        act = active.get(s, set())
        new = sum(1 for a in act if first_slice[a] == s)
        tx_n = sum(1 for r in txs if slice_of(r["close_time"]) == s)
        days.append({"slice": s, "active": len(act), "new": new, "returning": len(act) - new, "tx": tx_n})
    retention = []
    for c in range(nslices):
        cohort = {a for a, f in first_slice.items() if f == c}
        retention.append({"cohort": c, "size": len(cohort),
                          "active": [len(cohort & active.get(k, set())) if k >= c else None
                                     for k in range(nslices)]})

    per = {}
    for acct, rows in by_sender.items():
        times = sorted({int(r["close_time"]) for r in rows})
        st = gap_stats(times)
        st["class"] = classify(st)
        st["tx"] = len(rows)
        st["fee_drops"] = sum(int(r["fee_drops"]) for r in rows)
        st["failed"] = sum(1 for r in rows if r["result"] != "tesSUCCESS")
        st["top_type"] = Counter(r["tx_type"] for r in rows).most_common(1)[0][0]
        per[acct] = st

    tot_tx = len(txs)
    tot_fee = sum(int(r["fee_drops"]) for r in txs)
    tot_fail = sum(1 for r in txs if r["result"] != "tesSUCCESS")
    shares = {}
    for c in CLASSES:
        ss = [s for s in per.values() if s["class"] == c]
        tx_c = sum(s["tx"] for s in ss)
        fee_c = sum(s["fee_drops"] for s in ss)
        fail_c = sum(s["failed"] for s in ss)
        shares[c] = {
            "senders": len(ss), "tx": tx_c, "fee_drops": fee_c, "failed": fail_c,
            "sender_share": len(ss) / len(per) if per else 0.0,
            "tx_share": tx_c / tot_tx if tot_tx else 0.0,
            "fee_share": fee_c / tot_fee if tot_fee else 0.0,
            "failed_share": fail_c / tot_fail if tot_fail else 0.0,
            "fail_rate": fail_c / tx_c if tx_c else 0.0,
        }
    periodic = sorted(((a, s) for a, s in per.items() if s["class"] == "periodic"),
                      key=lambda x: (-x[1]["tx"], x[0]))
    top = [{"account": truncate(a), "tx": s["tx"], "events": s["n"], "period_s": s["median_gap"],
            "cv": round(s["cv"], 4), "period_share": round(s["period_share"], 4), "top_type": s["top_type"]}
           for a, s in periodic[:top_n]]
    return {
        "coverage": cov,
        "window": {"first_close": t0, "last_close": t1, "hours": round((t1 - t0) / 3600, 3),
                   "slices": nslices},
        "totals": {"tx": tot_tx, "senders": len(per), "fee_drops": tot_fee, "failed": tot_fail,
                   "ledger_txn_count": sum(int(r["txn_count"]) for r in ledgers)},
        "rules": {"MIN_EVENTS": MIN_EVENTS, "PERIODIC_CV": PERIODIC_CV, "PERIOD_TOL": PERIOD_TOL,
                  "PERIOD_SHARE": PERIOD_SHARE, "BURSTY_B": BURSTY_B},
        "days": days,
        "retention": retention,
        "shares": shares,
        "top_periodic": top,
        "per_sender": per,
    }


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data", default="data/senders/tx")
    p.add_argument("--json", default=None, help="write the full result (incl. per-sender stats) here")
    p.add_argument("--top", type=int, default=10)
    a = p.parse_args(argv)
    res = analyze(read_csv(os.path.join(a.data, "tx.csv")), read_csv(os.path.join(a.data, "ledgers.csv")),
                  read_csv(os.path.join(a.data, "gaps.csv")), a.top)
    if a.json:
        with open(a.json, "w") as fh:
            json.dump(res, fh, indent=1, sort_keys=True)
            fh.write("\n")
    cov, tot = res["coverage"], res["totals"]
    print(f"coverage ledgers {cov['first']}-{cov['last']} fetched={cov['fetched']} gaps={cov['gaps']} "
          f"holes={len(cov['holes'])} duplicates={cov.get('duplicates', 0)}")
    print(f"window {res['window']['hours']} h, {res['window']['slices']} slices; tx={tot['tx']} "
          f"(ledger txn_count sum {tot['ledger_txn_count']}) senders={tot['senders']} "
          f"fees={tot['fee_drops']} drops failed={tot['failed']}")
    for d in res["days"]:
        print(f"slice {d['slice']}: active={d['active']} new={d['new']} returning={d['returning']} "
              f"tx={d['tx']}")
    for r in res["retention"]:
        cells = " ".join("-" if v is None else str(v) for v in r["active"])
        print(f"cohort {r['cohort']} size={r['size']}: {cells}")
    for c, s in res["shares"].items():
        print(f"class {c}: senders={s['senders']} ({s['sender_share']:.1%}) "
              f"tx={s['tx']} ({s['tx_share']:.1%}) "
              f"fees={s['fee_share']:.1%} failed={s['failed']} ({s['failed_share']:.1%} of failures, "
              f"rate {s['fail_rate']:.2%})")
    for t in res["top_periodic"]:
        print(f"top periodic {t['account']} tx={t['tx']} period={t['period_s']}s cv={t['cv']} "
              f"share={t['period_share']} type={t['top_type']}")
    if cov["holes"] or cov.get("duplicates"):
        print(f"INCOMPLETE: {len(cov['holes'])} unlisted holes, {cov.get('duplicates', 0)} duplicate ledgers")
        return 1
    if tot["tx"] != tot["ledger_txn_count"]:
        print(f"INCOMPLETE: tx rows {tot['tx']} != ledger txn_count sum {tot['ledger_txn_count']}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
