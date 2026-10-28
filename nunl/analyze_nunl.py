#!/usr/bin/env python3
"""NegativeUNL history + disable/re-enable risk analyzer (read-only, Python 3 stdlib only).

Inputs
  --hist  nunl.jsonl from collect_nunl.py (flag-ledger UNLModify txs + NegativeUNL objects)
  --vals  vl.jsonl from vlatency/collect_vlatency.py (live validations + validated ledgers seen
          by this node), optional: needed for the 256-ledger validation-share risk tables
  --unl   UNL snapshot from govern/unl_snapshot.sh (trusted master keys, master -> signing key)

Thresholds are the postfiatd constants (src/xrpld/app/misc/NegativeUNLVote.h, FLAG_LEDGER_INTERVAL
= 256): a trusted validator with FEWER than LOW = 128 validations (50%) in the 256-ledger window
ending at a voting ledger is a disable candidate; a listed validator needs MORE than HIGH = 204
(80%) to be re-enabled; a node only votes with more than MIN_LOCAL = 230 (90%) validations of its
own; at most ceil(25% of the UNL) validators can be listed.

Output: REPORT.md, summary.json, risk.csv in --out; exit 0 when every consistency check passes,
1 when any fails (the corrupted-reconstruction negative control relies on this).
"""

import argparse
import csv
import hashlib
import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from pftstats.stats import summarize  # noqa: E402

FLAG = 256
LOW = FLAG * 50 // 100  # negativeUNLLowWaterMark
HIGH = FLAG * 80 // 100  # negativeUNLHighWaterMark
MIN_LOCAL = FLAG * 90 // 100  # negativeUNLMinLocalValsToVote
MAX_LISTED = 0.25  # negativeUNLMaxListed
WATCH = FLAG * 60 // 100  # report-only margin: 50-60% is "watch", not a postfiatd constant
RIPPLE_EPOCH = 946684800
B58 = "rpshnaf39wBUDNEGHJKLM4PQRST7VWXYZ2bcdeCg65jkm8oFqi1tuvAxyz"


def node_key_b58(hexkey):
    """33-byte node public key (hex, as in ledger objects) -> base58 'n...' form (token type 0x1C)."""
    raw = bytes([0x1C]) + bytes.fromhex(hexkey)
    raw += hashlib.sha256(hashlib.sha256(raw).digest()).digest()[:4]
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = B58[r] + out
    pad = len(raw) - len(raw.lstrip(b"\0"))
    return B58[0] * pad + out


def key(k):
    return node_key_b58(k) if k and all(c in "0123456789abcdefABCDEF" for c in k) else k


def iso(ripple_t):
    if ripple_t is None:
        return "?"
    return datetime.fromtimestamp(ripple_t + RIPPLE_EPOCH, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def classify(score, listed):
    """Risk status of one validator for one 256-ledger window, as postfiatd would vote on it."""
    if listed:
        return "REENABLE" if score > HIGH else "STAYS_LISTED"
    if score < LOW:
        return "DISABLE_CANDIDATE"
    if score < WATCH:
        return "WATCH"
    return "OK"


def load_jsonl(path):
    evs, bad = [], 0
    with open(path) as fh:
        for line in fh:
            try:
                evs.append(json.loads(line))
            except ValueError:
                bad += 1
    return evs, bad


def history(evs):
    """Flag events -> ordered flags, coverage, consistency checks, per-validator timelines."""
    flags = {}
    sessions = [e for e in evs if e.get("ev") == "session"]
    for e in evs:
        if e.get("ev") == "flag":
            flags[e["li"]] = e
    errors = {e["li"] for e in evs if e.get("ev") == "error"} - set(flags)
    lo = min(s["from"] for s in sessions) if sessions else min(flags, default=0)
    hi = max(s["to"] for s in sessions) if sessions else max(flags, default=0)
    first = -(-lo // FLAG) * FLAG
    expected = list(range(first, hi, FLAG))
    missing = [f for f in expected if f not in flags]
    order = sorted(flags)
    checks, events = [], []
    timeline = defaultdict(list)
    prev = None
    for f in order:
        e = flags[f]
        cur, nxt = e.get("nunl"), e.get("next")
        dis = set((cur or {}).get("disabled", []))
        # 1. the object never changes between a flag ledger and the following ledger
        if cur != nxt:
            checks.append(f"F{f}: NegativeUNL at F+1 differs from F ({cur} vs {nxt})")
        # 2. UNLModify txs in F are exactly the pending entries of the object at F
        want_dis = sorted(m["validator"] for m in e["mods"] if m.get("disabling") == 1)
        want_re = sorted(m["validator"] for m in e["mods"] if m.get("disabling") == 0)
        have_dis = [cur["to_disable"]] if cur and cur.get("to_disable") else []
        have_re = [cur["to_reenable"]] if cur and cur.get("to_reenable") else []
        if want_dis != have_dis or want_re != have_re:
            checks.append(f"F{f}: UNLModify txs {want_dis}/{want_re} != pending {have_dis}/{have_re}")
        for m in e["mods"]:
            if m.get("seq") not in (None, f):
                checks.append(f"F{f}: UNLModify LedgerSequence {m.get('seq')} != {f}")
        # 3. the list at F = list at F-256 + to_disable(F-256) - to_reenable(F-256)
        if prev is not None and prev["li"] == f - FLAG:
            p = prev.get("nunl") or {}
            exp = set(p.get("disabled", []))
            if p.get("to_disable"):
                exp.add(p["to_disable"])
            if p.get("to_reenable"):
                exp.discard(p["to_reenable"])
            if exp != dis:
                checks.append(f"F{f}: DisabledValidators {sorted(dis)} != expected {sorted(exp)}")
            for v in sorted(dis - set(p.get("disabled", []))):
                timeline[key(v)].append(("disabled", f, e.get("ct")))
            for v in sorted(set(p.get("disabled", [])) - dis):
                timeline[key(v)].append(("reenabled", f, e.get("ct")))
        for m in e["mods"]:
            act = "vote_disable" if m.get("disabling") == 1 else "vote_reenable"
            timeline[key(m["validator"])].append((act, f, e.get("ct")))
            events.append(
                {
                    "li": f,
                    "time": iso(e.get("ct")),
                    "validator": key(m["validator"]),
                    "action": act,
                    "tx": m.get("hash"),
                }
            )
        prev = e
    last = flags[order[-1]] if order else None
    current = (last or {}).get("nunl") or {"disabled": [], "to_disable": None, "to_reenable": None}
    spans = {}
    for v, tl in timeline.items():
        start, out = None, []
        for act, f, ct in tl:
            if act == "disabled":
                start = (f, ct)
            elif act == "reenabled" and start:
                out.append(
                    {
                        "from": start[0],
                        "to": f,
                        "ledgers": f - start[0],
                        "hours": round((ct - start[1]) / 3600, 2) if ct and start[1] else None,
                    }
                )
                start = None
        if start:
            out.append(
                {"from": start[0], "to": None, "ledgers": order[-1] - start[0], "hours": None, "open": True}
            )
        spans[v] = out
    return {
        "range": [lo, hi],
        "expected_flags": len(expected),
        "scanned_flags": len(order),
        "first_flag": order[0] if order else None,
        "last_flag": order[-1] if order else None,
        "first_time": iso(flags[order[0]].get("ct")) if order else None,
        "last_time": iso(flags[order[-1]].get("ct")) if order else None,
        "missing": missing,
        "errors_unresolved": sorted(errors),
        "unlmodify_txs": len(events),
        "flags_with_object": sum(1 for f in order if flags[f].get("nunl")),
        "events": events,
        "timeline": {v: [list(x) for x in tl] for v, tl in timeline.items()},
        "spans": spans,
        "current": {
            "disabled": [key(k) for k in current.get("disabled", [])],
            "to_disable": key(current.get("to_disable")),
            "to_reenable": key(current.get("to_reenable")),
        },
        "check_failures": checks,
    }


def load_unl(path):
    with open(path) as fh:
        u = json.load(fh)
    return {
        "t": u.get("t"),
        "quorum": u.get("validation_quorum"),
        "keys": sorted(u.get("trusted_validator_keys") or []),
        "signing": {sk: mk for mk, sk in (u.get("signing_keys") or {}).items()},
    }


def risk(vals, unl, listed):
    """256-ledger windows ending at each voting ledger V (V+1 is a flag ledger) fully seen by the
    ledger stream: score = ledgers in [V-255, V] for which the validator signed the validated hash."""
    chain, signed = {}, defaultdict(set)
    for e in vals:
        if e.get("ev") == "ledger":
            chain[e["li"]] = e["lh"]
    for e in vals:
        if e.get("ev") == "val":
            mk = e.get("mk") or unl["signing"].get(e["k"]) or e["k"]
            if chain.get(e["li"]) == e["lh"]:
                signed[mk].add(e["li"])
    windows = []
    for v in sorted(li for li in chain if (li + 1) % FLAG == 0):
        span = range(v - FLAG + 1, v + 1)
        if all(li in chain for li in span):
            windows.append(v)
    rows = []
    for v in windows:
        lo = v - FLAG + 1
        for k in unl["keys"]:
            s = sum(1 for li in signed.get(k, ()) if lo <= li <= v)
            rows.append(
                {
                    "window_end": v,
                    "validator": k,
                    "score": s,
                    "share": round(s / FLAG, 4),
                    "listed": k in listed,
                    "status": classify(s, k in listed),
                }
            )
    return windows, rows, min(chain, default=None), max(chain, default=None)


def score_stats(rows):
    return summarize([r["score"] for r in rows]) if rows else None


def write_report(out, h, unl, windows, rows, vrange, ok):
    os.makedirs(out, exist_ok=True)
    per = defaultdict(list)
    for r in rows:
        per[r["validator"]].append(r)
    summary_rows = []
    for k in unl["keys"] if unl else []:
        rs = per.get(k, [])
        if not rs:
            continue
        last = rs[-1]
        mn = min(rs, key=lambda r: r["score"])
        summary_rows.append(
            {
                "validator": k,
                "windows": len(rs),
                "last_score": last["score"],
                "last_share": last["share"],
                "min_score": mn["score"],
                "min_window": mn["window_end"],
                "last_status": last["status"],
                "worst_status": classify(mn["score"], last["listed"]),
                "margin_to_disable": last["score"] - LOW if not last["listed"] else None,
            }
        )
    summary_rows.sort(key=lambda r: (r["min_score"], r["validator"]))
    with open(os.path.join(out, "risk.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["window_end", "validator", "score", "share", "listed", "status"])
        w.writeheader()
        w.writerows(rows)
    summ = {
        "thresholds": {
            "flag_interval": FLAG,
            "low_disable_lt": LOW,
            "high_reenable_gt": HIGH,
            "min_local_votes_gt": MIN_LOCAL,
            "max_listed_frac": MAX_LISTED,
            "watch_lt": WATCH,
        },
        "history": {k: v for k, v in h.items() if k not in ("timeline",)},
        "unl": {
            "t": unl["t"],
            "size": len(unl["keys"]),
            "quorum": unl["quorum"],
            "max_listed": math.ceil(len(unl["keys"]) * MAX_LISTED),
        }
        if unl
        else None,
        "risk": {
            "score_stats": score_stats(rows),
            "windows": windows,
            "ledger_range": vrange,
            "validators": summary_rows,
        },
        "ok": ok,
    }
    with open(os.path.join(out, "summary.json"), "w") as fh:
        json.dump(summ, fh, indent=1, sort_keys=True)
    L = []
    a = L.append
    a("# NegativeUNL history and risk report\n")
    a(
        f"Scanned flag ledgers: **{h['scanned_flags']}/{h['expected_flags']}** in ledger range "
        f"**{h['range'][0]}-{h['range'][1]}** (first flag {h['first_flag']} at {h['first_time']}, last flag "
        f"{h['last_flag']} at {h['last_time']}); missing {len(h['missing'])}, unresolved errors "
        f"{len(h['errors_unresolved'])}.\n"
    )
    a(
        f"UNLModify pseudo-transactions found: **{h['unlmodify_txs']}**; flag ledgers with a "
        f"NegativeUNL object: "
        f"**{h['flags_with_object']}**.\n"
    )
    if not h["unlmodify_txs"] and not h["flags_with_object"]:
        a(
            f"**No NegativeUNL events exist in the scanned history**: no validator was voted onto or off the "
            f"NegativeUNL in ledgers {h['range'][0]}-{h['range'][1]} ({h['scanned_flags']} flag ledgers), "
            f"and the "
            f"NegativeUNL object did not exist at any of them.\n"
        )
    c = h["current"]
    a(
        f"Current state (last flag ledger): disabled {len(c['disabled'])} {c['disabled']}, pending disable "
        f"{c['to_disable']}, pending re-enable {c['to_reenable']}.\n"
    )
    a("## Consistency checks\n")
    a(
        "1. NegativeUNL object at F+1 equals F (changes only at flag ledgers)\n"
        "2. UNLModify txs in F match the object's pending ValidatorToDisable/ValidatorToReEnable at F\n"
        "3. DisabledValidators(F) = DisabledValidators(F-256) + ToDisable(F-256) - ToReEnable(F-256)\n"
    )
    a(f"Result: **{'PASS' if not h['check_failures'] else 'FAIL'}** ({len(h['check_failures'])} failures)\n")
    for x in h["check_failures"][:50]:
        a(f"- {x}")
    a("\n## Event timeline\n")
    if h["events"]:
        a("| flag ledger | time (UTC) | validator | action |\n|---|---|---|---|")
        for e in h["events"]:
            a(f"| {e['li']} | {e['time']} | `{e['validator']}` | {e['action']} |")
        a("\n| validator | disabled from | to | ledgers | hours |\n|---|---|---|---|---|")
        for v, ss in h["spans"].items():
            for s in ss:
                a(f"| `{v}` | {s['from']} | {s['to'] or 'still listed'} | {s['ledgers']} | {s['hours']} |")
    else:
        a("No events.\n")
    a("\n## Validation-share risk (256-ledger windows ending at voting ledgers)\n")
    a(
        f"Thresholds from postfiatd NegativeUNLVote.h: disable candidate if score < {LOW} (50%), "
        f"re-enable if "
        f"score > {HIGH} (80%); a node votes only with > {MIN_LOCAL} own validations; at most "
        f"ceil(25% x UNL) listed. WATCH (< {WATCH}, 60%) is a report-only margin.\n"
    )
    if unl:
        a(
            f"UNL snapshot {unl['t']}: {len(unl['keys'])} trusted validators, quorum {unl['quorum']}, "
            f"max listed "
            f"{math.ceil(len(unl['keys']) * MAX_LISTED)}.\n"
        )
    if windows:
        a(
            f"Complete windows seen by this node: **{len(windows)}** "
            f"(voting ledgers {windows[0]}-{windows[-1]}, "
            f"validation stream ledgers {vrange[0]}-{vrange[1]}).\n"
        )
        a(
            "| validator | windows | last score | last share | min score | min window | worst status | "
            "margin to disable |\n|---|---|---|---|---|---|---|---|"
        )
        for r in summary_rows:
            a(
                f"| `{r['validator']}` | {r['windows']} | {r['last_score']} | {r['last_share']:.1%} | "
                f"{r['min_score']} | "
                f"{r['min_window']} | {r['worst_status']} | {r['margin_to_disable']} |"
            )
        cnt = defaultdict(int)
        for r in summary_rows:
            cnt[r["worst_status"]] += 1
        a(f"\nWorst status counts: {dict(sorted(cnt.items()))}\n")
        st = score_stats(rows)
        a(
            f"Scores over all {st['count']} (window, validator) pairs (pftstats.summarize): min {st['min']}, "
            f"median {st['median']:g}, p95 {st['p95']:g}, max {st['max']}, mean {st['mean']:.2f}.\n"
        )
    else:
        a("No complete 256-ledger window in the validation data (or no --vals given).\n")
    a(f"\nOverall: **{'PASS' if ok else 'FAIL'}**\n")
    with open(os.path.join(out, "REPORT.md"), "w") as fh:
        fh.write("\n".join(L) + "\n")
    return summ


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--hist", required=True)
    ap.add_argument("--vals")
    ap.add_argument("--unl")
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    evs, bad = load_jsonl(args.hist)
    h = history(evs)
    unl = load_unl(args.unl) if args.unl else None
    windows, rows, vrange = [], [], [None, None]
    if args.vals and unl:
        vals, _ = load_jsonl(args.vals)
        listed = set(h["current"]["disabled"])
        windows, rows, a, b = risk(vals, unl, listed)
        vrange = [a, b]
    ok = not h["check_failures"] and not h["missing"] and not h["errors_unresolved"] and bad == 0
    write_report(args.out, h, unl, windows, rows, vrange, ok)
    print(
        f"scanned_flags={h['scanned_flags']}/{h['expected_flags']} range={h['range'][0]}-{h['range'][1]} "
        f"missing={len(h['missing'])} errors={len(h['errors_unresolved'])} bad_lines={bad} "
        f"unlmodify={h['unlmodify_txs']} objects={h['flags_with_object']} "
        f"current_disabled={len(h['current']['disabled'])} check_failures={len(h['check_failures'])} "
        f"risk_windows={len(windows)} -> {'PASS' if ok else 'FAIL'}"
    )
    for x in h["check_failures"][:20]:
        print(f"  CHECK FAIL {x}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
