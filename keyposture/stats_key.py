#!/usr/bin/env python3
"""Signing-key posture statistics for one reconciled window (Python 3 stdlib; matplotlib only for --chart).

    stats_key.py --reconcile R.json --snap-a A.jsonl.gz --snap-b B.jsonl.gz
                 [--rewardflow data/rewardflow/data.jsonl] [--json OUT] [--chart PNG] [--md OUT.md]

R.json is reconcile_key.py --json output for the same snapshots. Stats are only computed from a
PASSING reconciliation (no residuals, not --negative); anything else exits 2.

Sections (offline, deterministic, same inputs -> byte-identical JSON):
  classes      per class at A and B: accounts, share of accounts, PFT held, share of PFT
  weak         weak-configuration flags (posture_key.weaknesses) per snapshot, with accounts
  top          the 10 largest balances per class at B
  transitions  class changes A->B from the reconciliation (incl. created / deleted)
  rewardflow   posture at A and B of the Task Node reward payer and every recipient found by
               rewardflow/collect_rewardflow.py (payer = the data file's account)
  buckets      accounts per balance bucket and class at B (the chart)
"""

import argparse
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import reconcile_key as R  # noqa: E402
from posture_key import CLASSES, classify, weaknesses  # noqa: E402

DROPS = 1_000_000
BUCKETS = [(0, 20), (20, 100), (100, 1_000), (1_000, 10_000), (10_000, 100_000), (100_000, None)]
# reference categorical palette (dataviz skill palette.md, light), fixed order by class;
# the 9th class (no_usable_key) is not given a 9th hue: neutral gray
COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948", "#8a8985"]


def short(a):
    return a[:6] + "…"


def bucket_name(lo, hi):
    return f"{lo:,}+" if hi is None else f"{lo:,}-{hi:,}"


def class_table(accts, post):
    n, bal = Counter(), Counter()
    for a, r in accts.items():
        c = classify(post[a])
        n[c] += 1
        bal[c] += int(r["Balance"])
    tn, tb = sum(n.values()), sum(bal.values())
    return {
        c: {
            "accounts": n[c],
            "share": round(n[c] / tn, 6) if tn else 0.0,
            "pft": bal[c] / DROPS,
            "pft_share": round(bal[c] / tb, 6) if tb else 0.0,
        }
        for c in CLASSES
    }


def weak_table(post):
    out = {}
    for a, p in sorted(post.items()):
        for w in weaknesses(a, p):
            out.setdefault(w, []).append(a)
    return {w: {"n": len(v), "accounts": v} for w, v in sorted(out.items())}


def rewardflow_accounts(path):
    payer, rec = None, set()
    with open(path) as fh:
        for line in fh:
            x = json.loads(line)
            payer = payer or x.get("account")
            if x.get("kind") == "tx" and x.get("direction") == "out" and x.get("destination"):
                rec.add(x["destination"])
    return payer, sorted(rec - {payer})


def compute(res, snap_a, snap_b, rewardflow=None):
    if res.get("result") != "PASS" or res.get("negative"):
        raise ValueError("reconciliation did not pass (or is a --negative run)")
    ha, aa, sla = R.load_snapshot(snap_a)
    hb, ab, slb = R.load_snapshot(snap_b)
    if (ha["ledger_index"], hb["ledger_index"]) != (
        res["snap_a"]["ledger_index"],
        res["snap_b"]["ledger_index"],
    ):
        raise ValueError("snapshots do not match the reconciliation")
    pa, pb = R.postures(aa, sla), R.postures(ab, slb)
    top = {}
    for c in CLASSES:
        rows = sorted(((int(ab[a]["Balance"]), a) for a in ab if classify(pb[a]) == c), reverse=True)[:10]
        if rows:
            top[c] = [{"account": short(a), "pft": b / DROPS} for b, a in rows]
    buckets = []
    for lo, hi in BUCKETS:
        cnt = Counter(
            classify(pb[a])
            for a, r in ab.items()
            if int(r["Balance"]) >= lo * DROPS and (hi is None or int(r["Balance"]) < hi * DROPS)
        )
        buckets.append({"bucket": bucket_name(lo, hi), **{c: cnt[c] for c in CLASSES}})
    out = {
        "snap_a": res["snap_a"],
        "snap_b": res["snap_b"],
        "classes_a": class_table(aa, pa),
        "classes_b": class_table(ab, pb),
        "weak_a": weak_table(pa),
        "weak_b": weak_table(pb),
        "top_b": top,
        "transitions": res["transitions"],
        "created": res["created"],
        "deleted": res["deleted"],
        "buckets_b": buckets,
    }
    if rewardflow:
        payer, rec = rewardflow_accounts(rewardflow)
        rows = []
        for role, a in [("payer", payer)] + [("recipient", r) for r in rec]:
            rows.append(
                {
                    "role": role,
                    "account": short(a),
                    "class_a": classify(pa[a]) if a in pa else "absent",
                    "class_b": classify(pb[a]) if a in pb else "absent",
                    "weak_b": weaknesses(a, pb[a]) if a in pb else [],
                    "pft_b": int(ab[a]["Balance"]) / DROPS if a in ab else 0.0,
                }
            )
        out["rewardflow"] = rows
    return out


def chart(stats, path):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    rows = stats["buckets_b"]
    present = [c for c in CLASSES if any(r[c] for r in rows)]
    labels = [r["bucket"] for r in rows]
    fig, ax = plt.subplots(figsize=(8, 4.2), dpi=120)
    fig.patch.set_facecolor("#fcfcfb")
    ax.set_facecolor("#fcfcfb")
    bottom = [0] * len(rows)
    for c in present:
        vals = [r[c] for r in rows]
        ax.bar(
            labels,
            vals,
            bottom=bottom,
            width=0.6,
            color=COLORS[CLASSES.index(c)],
            label=c,
            edgecolor="#fcfcfb",
            linewidth=2,
        )
        bottom = [b + v for b, v in zip(bottom, vals, strict=True)]
    for i, t in enumerate(bottom):
        ax.text(i, t, f"{t:,}", ha="center", va="bottom", fontsize=9, color="#0b0b0b")
    sb = stats["snap_b"]
    one = f" (all {present[0]})" if len(present) == 1 else ""
    ax.set_title(
        f"Accounts by PFT balance at ledger {sb['ledger_index']}, by signing-key class{one}",
        fontsize=10,
        color="#0b0b0b",
        loc="left",
    )
    ax.set_xlabel("balance (PFT)", color="#52514e")
    ax.set_ylabel("accounts", color="#52514e")
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color("#b5b4ad")
    ax.tick_params(colors="#52514e")
    ax.yaxis.grid(True, color="#e6e5e0", linewidth=0.8)
    ax.set_axisbelow(True)
    if len(present) > 1:
        ax.legend(frameon=False, fontsize=9)
    fig.tight_layout()
    fig.savefig(path, facecolor=fig.get_facecolor())
    plt.close(fig)


def markdown(s):
    L = []
    L.append("| class | A accounts | A share | B accounts | B share | B PFT | B PFT share |")
    L.append("|---|---:|---:|---:|---:|---:|---:|")
    for c in CLASSES:
        a, b = s["classes_a"][c], s["classes_b"][c]
        L.append(
            f"| {c} | {a['accounts']} | {a['share']:.2%} | {b['accounts']} | {b['share']:.2%} | "
            f"{b['pft']:,.6f} | {b['pft_share']:.2%} |"
        )
    L.append("")
    L.append(
        "Weak configurations at B: " + (", ".join(f"{w} {v['n']}" for w, v in s["weak_b"].items()) or "none")
    )
    L.append("")
    L.append(
        f"Transitions A->B (created {s['created']}, deleted {s['deleted']}): "
        + (", ".join(f"{t['from']} -> {t['to']}: {t['n']}" for t in s["transitions"]) or "none")
    )
    if "rewardflow" in s:
        L.append("")
        L.append("| role | account | class A | class B | weak flags B | PFT at B |")
        L.append("|---|---|---|---|---|---:|")
        for r in s["rewardflow"]:
            L.append(
                f"| {r['role']} | `{r['account']}` | {r['class_a']} | {r['class_b']} | "
                f"{', '.join(r['weak_b']) or '-'} | {r['pft_b']:,.6f} |"
            )
    L.append("")
    for c, rows in s["top_b"].items():
        L.append(f"Top balances at B, {c}: " + ", ".join(f"`{r['account']}` {r['pft']:,.2f}" for r in rows))
    return "\n".join(L) + "\n"


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--reconcile", required=True)
    p.add_argument("--snap-a", required=True)
    p.add_argument("--snap-b", required=True)
    p.add_argument("--rewardflow")
    p.add_argument("--json")
    p.add_argument("--chart")
    p.add_argument("--md")
    a = p.parse_args(argv)
    with open(a.reconcile) as fh:
        res = json.load(fh)
    try:
        s = compute(res, a.snap_a, a.snap_b, a.rewardflow)
    except (ValueError, R.Incomplete, OSError) as e:
        print(f"REFUSED {e}")
        return 2
    if a.json:
        with open(a.json, "w") as fh:
            json.dump(s, fh, indent=1, sort_keys=True)
            fh.write("\n")
    md = markdown(s)
    if a.md:
        with open(a.md, "w") as fh:
            fh.write(md)
    if a.chart:
        chart(s, a.chart)
    print(md, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
