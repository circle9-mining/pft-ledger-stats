#!/usr/bin/env python3
"""Controls for the periodic / bursty / sporadic rule in analyze_senders.py (stdlib only, seeded).

Positive controls (synthetic senders over a 72 h window, times snapped to a 4 s ledger grid):
  periodic  fixed period drawn from 60..3600 s, each event jittered by +-5 % of the period
  poisson   exponential gaps, mean drawn from 300..3600 s  (expected class: sporadic)
  burst     bursts of 5..15 events 2..8 s apart, burst starts Poisson with mean 2..8 h (expected: bursty)
Negative control (--data): every real sender keeps its number of distinct event ledgers, but the
  times are drawn without replacement from all ledger close times of the collection, which
  destroys any schedule. Real periodic senders must not stay periodic.

Pass: each positive control classifies >= --min-correct (default 0.95) of its senders as expected,
and the negative control leaves <= --max-neg-periodic (default 0.01) of eligible senders
(>= MIN_EVENTS events) periodic. Exit 0 on PASS, 1 on FAIL.
"""

import argparse
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from analyze_senders import MIN_EVENTS, classify, gap_stats, read_csv  # noqa: E402

WINDOW = 72 * 3600
GRID = 4


def snap(ts):
    return sorted({int(t // GRID) * GRID for t in ts if 0 <= t < WINDOW})


def periodic(rng):
    p = rng.uniform(60, 3600)
    t0 = rng.uniform(0, p)
    return snap(t0 + k * p + rng.uniform(-0.05, 0.05) * p for k in range(int(WINDOW / p) + 1))


def poisson(rng):
    mean = rng.uniform(300, 3600)
    out, t = [], rng.expovariate(1 / mean)
    while t < WINDOW:
        out.append(t)
        t += rng.expovariate(1 / mean)
    return snap(out)


def burst(rng):
    mean = rng.uniform(2, 8) * 3600
    out, t = [], rng.expovariate(1 / mean)
    while t < WINDOW:
        u = t
        for _ in range(rng.randint(5, 15)):
            out.append(u)
            u += rng.uniform(2, 8)
        t += rng.expovariate(1 / mean)
    return snap(out)


GENERATORS = {
    "periodic": (periodic, "periodic"),
    "poisson": (poisson, "sporadic"),
    "burst": (burst, "bursty"),
}


def run_positive(name, n, seed):
    gen, want = GENERATORS[name]
    rng = random.Random(f"{seed}:{name}")
    got = [classify(gap_stats(gen(rng))) for _ in range(n)]
    ok = sum(1 for g in got if g == want)
    return {"control": name, "expected": want, "n": n, "correct": ok,
            "classes": {c: got.count(c) for c in sorted(set(got))}}


def run_negative(data, seed):
    txs = read_csv(os.path.join(data, "tx.csv"))
    ledgers = read_csv(os.path.join(data, "ledgers.csv"))
    pool = sorted({int(r["close_time"]) for r in ledgers})
    events = {}
    for r in txs:
        events.setdefault(r["account"], set()).add(int(r["close_time"]))
    rng = random.Random(f"{seed}:negative")
    real_p = shuf_p = eligible = 0
    for acct in sorted(events):
        times = sorted(events[acct])
        if len(times) < MIN_EVENTS:
            continue
        eligible += 1
        if classify(gap_stats(times)) == "periodic":
            real_p += 1
            if classify(gap_stats(sorted(rng.sample(pool, len(times))))) == "periodic":
                shuf_p += 1
        else:
            rng.sample(pool, len(times))  # keep the stream independent of the classification
    return {"control": "shuffled", "eligible": eligible, "real_periodic": real_p, "shuffled_periodic": shuf_p}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--n", type=int, default=200, help="synthetic senders per positive control")
    p.add_argument("--seed", default="senders-controls-1")
    p.add_argument("--data", default=None, help="collection dir for the shuffled-timestamp negative control")
    p.add_argument("--min-correct", type=float, default=0.95)
    p.add_argument("--max-neg-periodic", type=float, default=0.01)
    a = p.parse_args(argv)
    fail = 0
    for name in GENERATORS:
        r = run_positive(name, a.n, a.seed)
        ok = r["correct"] >= a.min_correct * r["n"]
        fail += not ok
        print(f"control {name}: expected {r['expected']} {r['correct']}/{r['n']} classes={r['classes']} "
              f"{'PASS' if ok else 'FAIL'}")
    if a.data:
        r = run_negative(a.data, a.seed)
        ok = r["shuffled_periodic"] <= a.max_neg_periodic * max(r["eligible"], 1)
        fail += not ok
        print(f"control shuffled: eligible={r['eligible']} real periodic={r['real_periodic']} "
              f"periodic after shuffle={r['shuffled_periodic']} {'PASS' if ok else 'FAIL'}")
    print(f"RESULT {'PASS' if not fail else f'FAIL ({fail} controls)'}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
