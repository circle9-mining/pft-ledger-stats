#!/usr/bin/env python3
"""Analyze one ledger-state snapshot written by snapshot.py (offline, deterministic).

Reads snapshot-<ledger>.jsonl.gz (header line + one ledger object per line) and writes
REPORT.md, report.json and (unless --no-charts) charts/*.png into --out:

  - object counts by LedgerEntryType; summed counts must equal objects_paged (exit 1 if not)
  - account count (AccountRoot) and PFT balance distribution: total, median, p90, p99,
    top-10 / top-100 share, Gini
  - reserve-locked vs spendable PFT: locked = min(Balance, base + inc x OwnerCount) with
    base/inc from the snapshot's own FeeSettings object (compared against the server_state
    reserve_base/reserve_inc recorded at pin time)
  - OwnerCount buckets
  - per-holder trust lines (RippleState, both sides), offers (Offer, NFTokenOffer) and other
    owned objects (any object with Account/Owner, NFTokenPage by its owner prefix)
  - sum of AccountRoot balances vs the ledger header total_coins, with PFT held in other
    objects (Escrow, PayChannel) listed as the explanation of any difference

The same snapshot always gives byte-identical REPORT.md / report.json (no wall-clock
times, sorted keys, fixed float formatting). The report holds aggregates only: no address
is printed.

Usage:
  ./analyze.py data/state/snapshot-N.jsonl.gz --out state/report [--no-charts]
"""

import argparse
import gzip
import hashlib
import json
import os
import sys
from collections import Counter, defaultdict

DROPS = 1_000_000
OWNER_BUCKETS = [(0, 0), (1, 1), (2, 4), (5, 9), (10, 24), (25, 99), (100, None)]
HOLDER_BUCKETS = [(1, 1), (2, 4), (5, 9), (10, 49), (50, None)]
B58 = "rpshnaf39wBUDNEGHJKLM4PQRST7VWXYZ2bcdeCg65jkm8oFqi1tuvAxyz"


def load(path):
    with gzip.open(path, "rt") as fh:
        header = json.loads(fh.readline())
        objs = [json.loads(line) for line in fh if line.strip()]
    return header, objs


def percentile(values, p):
    """Linear-interpolated percentile (numpy default) of a pre-sorted list."""
    k = (len(values) - 1) * p / 100.0
    lo = int(k)
    hi = min(lo + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (k - lo)


def gini(values):
    """Gini coefficient of non-negative values (0 = all equal, (n-1)/n = one holds all)."""
    s = sorted(values)
    n, total = len(s), sum(s)
    if n == 0 or total == 0:
        return 0.0
    return 2.0 * sum((i + 1) * x for i, x in enumerate(s)) / (n * total) - (n + 1) / n


def top_share(values, n):
    total = sum(values)
    return sum(sorted(values, reverse=True)[:n]) / total if total else 0.0


def account_id_to_address(hex40):
    """Classic address (base58check, XRPL alphabet) of a 20-byte account ID."""
    payload = b"\x00" + bytes.fromhex(hex40)
    data = payload + hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    n = int.from_bytes(data, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = B58[r] + out
    pad = len(data) - len(data.lstrip(b"\x00"))
    return B58[0] * pad + out


def bucket_label(lo, hi):
    if hi is None:
        return f"{lo}+"
    return str(lo) if lo == hi else f"{lo}-{hi}"


def bucketize(values, buckets):
    out = {}
    for lo, hi in buckets:
        out[bucket_label(lo, hi)] = sum(1 for v in values if v >= lo and (hi is None or v <= hi))
    return out


def reserves(objs, header):
    fees = [o for o in objs if o["LedgerEntryType"] == "FeeSettings"]
    if fees:
        f = fees[0]
        base = int(f.get("ReserveBaseDrops", f.get("ReserveBase")))
        inc = int(f.get("ReserveIncrementDrops", f.get("ReserveIncrement")))
        src = "FeeSettings"
    else:
        base, inc = header["server_state_reserve_base"], header["server_state_reserve_inc"]
        src = "server_state"
    return base, inc, src


def owners_of(o):
    """Accounts that own object o (for per-holder counts). RippleState has two sides."""
    t = o["LedgerEntryType"]
    if t == "RippleState":
        return [o["LowLimit"]["issuer"], o["HighLimit"]["issuer"]]
    if t == "NFTokenPage":
        return [account_id_to_address(o["index"][:40])]
    for k in ("Account", "Owner"):
        if k in o:
            return [o[k]]
    return []


def analyze(header, objs):
    types = Counter(o["LedgerEntryType"] for o in objs)
    counts_ok = sum(types.values()) == header["objects_paged"] == len(objs)
    accts = [o for o in objs if o["LedgerEntryType"] == "AccountRoot"]
    bal = sorted(int(o["Balance"]) for o in accts)
    base, inc, rsrc = reserves(objs, header)
    locked = spendable = 0
    for o in accts:
        b = int(o["Balance"])
        r = min(b, base + inc * int(o.get("OwnerCount", 0)))
        locked += r
        spendable += b - r
    total = sum(bal)

    held = defaultdict(int)  # native PFT in non-account objects
    for o in objs:
        t = o["LedgerEntryType"]
        if t == "Escrow" and isinstance(o.get("Amount"), str):
            held["Escrow Amount"] += int(o["Amount"])
        elif t == "PayChannel" and isinstance(o.get("Amount"), str):
            held["PayChannel Amount-Balance"] += int(o["Amount"]) - int(o.get("Balance", "0"))
    total_coins = header["total_coins"]

    per = defaultdict(lambda: Counter())
    for o in objs:
        t = o["LedgerEntryType"]
        if t == "AccountRoot":
            continue
        kind = "lines" if t == "RippleState" else ("offers" if t in ("Offer", "NFTokenOffer") else "other")
        for a in owners_of(o):
            per[a][kind] += 1
    known = {o["Account"] for o in accts}
    holders = {}
    for kind in ("lines", "offers", "other"):
        vals = sorted(c[kind] for a, c in per.items() if c[kind] and a in known)
        holders[kind] = {
            "accounts_with_any": len(vals),
            "objects": sum(vals),
            "max_per_account": vals[-1] if vals else 0,
            "median_among_holders": percentile(vals, 50) if vals else 0,
            "buckets": bucketize(vals, HOLDER_BUCKETS),
        }
    unowned = sum(1 for a in per if a not in known)  # e.g. issuer side rrrr... placeholders

    return {
        "ledger": {k: header[k] for k in ("ledger_index", "ledger_hash", "account_hash", "close_time_iso",
                                          "total_coins", "objects_paged")},
        "object_counts": dict(sorted(types.items(), key=lambda kv: (-kv[1], kv[0]))),
        "object_count_check": {"sum_of_types": sum(types.values()), "objects_paged": header["objects_paged"],
                               "lines_in_file": len(objs), "ok": counts_ok},
        "accounts": len(accts),
        "balance_drops": {
            "total": total, "min": bal[0], "median": percentile(bal, 50), "p90": percentile(bal, 90),
            "p99": percentile(bal, 99), "max": bal[-1], "mean": total / len(bal),
            "top10_share": top_share(bal, 10), "top100_share": top_share(bal, 100), "gini": gini(bal),
        },
        "reserve": {
            "source": rsrc, "base_drops": base, "inc_drops": inc,
            "server_state_base_drops": header["server_state_reserve_base"],
            "server_state_inc_drops": header["server_state_reserve_inc"],
            "matches_server_state": (base, inc) == (header["server_state_reserve_base"],
                                                    header["server_state_reserve_inc"]),
            "locked_drops": locked, "spendable_drops": spendable,
            "accounts_below_reserve": sum(1 for o in accts if int(o["Balance"]) <
                                          base + inc * int(o.get("OwnerCount", 0))),
        },
        "owner_count_buckets": bucketize([int(o.get("OwnerCount", 0)) for o in accts], OWNER_BUCKETS),
        "holders": holders,
        "owner_keys_without_accountroot": unowned,
        "total_coins_check": {
            "header_total_coins": total_coins, "sum_account_balances": total,
            "held_in_other_objects": dict(sorted(held.items())),
            "difference": total_coins - total - sum(held.values()),
        },
    }


def pft(drops):
    return f"{drops / DROPS:,.6f}"


def pct(x, digits=4):
    return f"{100 * x:.{digits}f}%"


def render(r, charts):
    b, rv, tc, lg = r["balance_drops"], r["reserve"], r["total_coins_check"], r["ledger"]
    L = [
        f"# Post Fiat ledger-state snapshot — ledger {lg['ledger_index']}",
        "",
        f"- ledger_hash `{lg['ledger_hash']}`",
        f"- account_hash `{lg['account_hash']}`",
        f"- close time {lg['close_time_iso']}; {lg['objects_paged']} objects paged with `ledger_data` "
        "(binary=false) from a local testnet node's admin RPC",
        "",
        "## Objects by LedgerEntryType",
        "",
        "| LedgerEntryType | objects | share |",
        "|---|---:|---:|",
    ]
    n = r["object_count_check"]["sum_of_types"]
    for t, c in r["object_counts"].items():
        L.append(f"| {t} | {c} | {pct(c / n)} |")
    oc = r["object_count_check"]
    L += ["", f"Sum of type counts {oc['sum_of_types']} = objects paged {oc['objects_paged']} = lines in "
          f"snapshot {oc['lines_in_file']}: **{'OK' if oc['ok'] else 'MISMATCH'}**", ""]
    if charts:
        L += ["![objects by type](charts/object_types.png)", ""]
    L += [
        f"## PFT balances ({r['accounts']} accounts)",
        "",
        "| stat | PFT |",
        "|---|---:|",
        f"| total in AccountRoots | {pft(b['total'])} |",
        f"| min | {pft(b['min'])} |",
        f"| median | {pft(b['median'])} |",
        f"| p90 | {pft(b['p90'])} |",
        f"| p99 | {pft(b['p99'])} |",
        f"| max | {pft(b['max'])} |",
        f"| mean | {pft(round(b['mean']))} |",
        "",
        f"- top-10 accounts hold **{pct(b['top10_share'])}**, top-100 hold **{pct(b['top100_share'])}**",
        f"- Gini coefficient **{b['gini']:.4f}** (0 = equal, 1 = one account holds everything)",
        "",
    ]
    if charts:
        L += ["![Lorenz curve](charts/lorenz.png)", ""]
    L += [
        "## Reserve-locked vs spendable",
        "",
        f"Reserve = {pft(rv['base_drops'])} base + {pft(rv['inc_drops'])} x OwnerCount (from {rv['source']}; "
        f"server_state at pin time: {pft(rv['server_state_base_drops'])} / "
        f"{pft(rv['server_state_inc_drops'])}, "
        f"{'match' if rv['matches_server_state'] else 'DIFFERENT'}). Locked = min(Balance, reserve).",
        "",
        "| | PFT | share |",
        "|---|---:|---:|",
        f"| reserve-locked | {pft(rv['locked_drops'])} | {pct(rv['locked_drops'] / b['total'], 6)} |",
        f"| spendable | {pft(rv['spendable_drops'])} | {pct(rv['spendable_drops'] / b['total'], 6)} |",
        "",
        f"{rv['accounts_below_reserve']} accounts hold less than their own reserve.",
        "",
        "## OwnerCount buckets",
        "",
        "| OwnerCount | accounts |",
        "|---|---:|",
    ]
    for k, v in r["owner_count_buckets"].items():
        L.append(f"| {k} | {v} |")
    L += [
        "",
        "## Per-holder objects",
        "",
        "Trust lines count once for each side; offers = Offer + NFTokenOffer; other = NFTokenPage (by its",
        "owner prefix), owner DirectoryNodes and any other object with an Account/Owner field.",
        "",
        "| kind | accounts with any | objects | max per account | median among holders | "
        + " | ".join(f"{k}" for k in r["holders"]["lines"]["buckets"]) + " |",
        "|---|---:|---:|---:|---:|" + "---:|" * len(r["holders"]["lines"]["buckets"]),
    ]
    for kind, h in r["holders"].items():
        L.append(f"| {kind} | {h['accounts_with_any']} | {h['objects']} | {h['max_per_account']} | "
                 f"{h['median_among_holders']:g} | "
                 + " | ".join(str(v) for v in h["buckets"].values()) + " |")
    L += ["", f"Owner keys that have no AccountRoot in this ledger: {r['owner_keys_without_accountroot']} "
          "(these are left out of the per-holder table).", "",
          "## total_coins vs sum of balances", "",
          "| | drops |", "|---|---:|",
          f"| ledger header total_coins | {tc['header_total_coins']} |",
          f"| sum of AccountRoot Balance | {tc['sum_account_balances']} |"]
    for k, v in tc["held_in_other_objects"].items():
        L.append(f"| held in {k} | {v} |")
    L += [f"| difference | {tc['difference']} |", ""]
    if tc["difference"] == 0:
        none = " (this ledger has no PFT in either)" if not tc["held_in_other_objects"] else ""
        L.append("total_coins equals the PFT in AccountRoots plus the PFT held in Escrow/PayChannel objects"
                 f"{none}: every drop is accounted for. Fees are burned, so total_coins only falls over "
                 "time.")
    else:
        L.append(f"**{tc['difference']} drops are not explained** by AccountRoot, Escrow or PayChannel "
                 "balances.")
    L.append("")
    return "\n".join(L)


def charts(r, bal, outdir):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    ink, ink2, grid, blue, surf = "#0b0b0b", "#52514e", "#e1e0d9", "#2a78d6", "#fcfcfb"
    os.makedirs(outdir, exist_ok=True)
    plt.rcParams.update({"font.size": 10, "axes.edgecolor": grid, "axes.labelcolor": ink2,
                         "xtick.color": ink2, "ytick.color": ink2, "text.color": ink})
    s = sorted(bal)
    tot = sum(s)
    xs, ys, acc = [0.0], [0.0], 0
    for i, v in enumerate(s):
        acc += v
        xs.append((i + 1) / len(s))
        ys.append(acc / tot)
    fig, ax = plt.subplots(figsize=(6, 5), facecolor=surf)
    ax.set_facecolor(surf)
    ax.plot([0, 1], [0, 1], color=ink2, linewidth=1, linestyle=(0, (3, 3)))
    ax.plot(xs, ys, color=blue, linewidth=2)
    ax.text(0.04, 0.9, f"Gini {r['balance_drops']['gini']:.4f}", color=ink)
    ax.text(0.55, 0.62, "equal distribution", color=ink2, rotation=38)
    ax.set_xlabel("share of accounts (poorest first)")
    ax.set_ylabel("share of PFT in AccountRoots")
    ax.set_title(f"PFT balance Lorenz curve, ledger {r['ledger']['ledger_index']}", loc="left")
    ax.grid(color=grid, linewidth=0.5)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    fig.tight_layout()
    fig.savefig(os.path.join(outdir, "lorenz.png"), dpi=110, facecolor=surf)
    plt.close(fig)

    items = list(r["object_counts"].items())[::-1]
    fig, ax = plt.subplots(figsize=(6.5, 0.38 * len(items) + 1.2), facecolor=surf)
    ax.set_facecolor(surf)
    ax.barh([k for k, _ in items], [v for _, v in items], color=blue, height=0.6)
    for i, (_, v) in enumerate(items):
        ax.text(v, i, f" {v}", va="center", color=ink2, fontsize=9)
    ax.set_xscale("log")
    ax.set_xlabel("objects (log scale)")
    ax.set_title(f"Ledger objects by type, ledger {r['ledger']['ledger_index']}", loc="left")
    ax.grid(axis="x", color=grid, linewidth=0.5)
    ax.set_axisbelow(True)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    fig.tight_layout()
    fig.savefig(os.path.join(outdir, "object_types.png"), dpi=110, facecolor=surf)
    plt.close(fig)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("snapshot")
    p.add_argument("--out", default="state/report")
    p.add_argument("--no-charts", action="store_true")
    args = p.parse_args(argv)
    header, objs = load(args.snapshot)
    r = analyze(header, objs)
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "report.json"), "w") as fh:
        json.dump(r, fh, sort_keys=True, indent=1)
        fh.write("\n")
    with open(os.path.join(args.out, "REPORT.md"), "w") as fh:
        fh.write(render(r, charts=True))
    if not args.no_charts:
        charts(r, [int(o["Balance"]) for o in objs if o["LedgerEntryType"] == "AccountRoot"],
               os.path.join(args.out, "charts"))
    oc = r["object_count_check"]
    print(f"ledger {header['ledger_index']}: {oc['sum_of_types']} objects, {r['accounts']} accounts, "
          f"gini {r['balance_drops']['gini']:.4f}, count check {'OK' if oc['ok'] else 'MISMATCH'}, "
          f"total_coins difference {r['total_coins_check']['difference']} drops -> {args.out}")
    return 0 if oc["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
