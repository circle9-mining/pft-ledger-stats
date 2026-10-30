#!/usr/bin/env python3
"""Write REPORT.md from diff.json (diff_churn.py) and funders.json (funders_churn.py).

Offline and deterministic: the same inputs give byte-identical output. Addresses and hashes
are truncated to 8 characters; drops are shown as PFT with 6 decimals.

With --chart PNG it also draws the per-account balance change chart (matplotlib, PNG written
without timestamps). --crosscheck LOG (repeatable) copies each SUMMARY line of a crosscheck run
log into the report; --tests TEXT records the test-suite result line.

Usage:
  ./report_churn.py diff.json funders.json --out acctchurn/REPORT.md [--top 10]
      [--chart acctchurn/balance-changes.png] [--crosscheck LOG ...] [--tests "N passed"]
"""

import argparse
import json
import os
import sys

DROPS = 1_000_000


def short(a):
    return (a or "?")[:8] + "…"


def pft(drops):
    sign = "-" if drops < 0 else ""
    q, r = divmod(abs(drops), DROPS)
    return f"{sign}{q:,}.{r:06d}"


def deltas(d):
    """(address, delta_drops, created?) for every AccountRoot whose balance changed, largest |delta| first."""
    rows = [(r["address"], r["balance_drops"], True) for r in d["accounts"]["created"]]
    rows += [(r["address"], -r["balance_drops"], False) for r in d["accounts"]["deleted"]]
    rows += [(r["address"], r["delta_drops"], False) for r in d["accounts"]["changed"] if r["delta_drops"]]
    return sorted(rows, key=lambda r: (-abs(r[1]), r[0]))


def chart(d, path, limit=40):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    rows = deltas(d)[:limit]
    rows.sort(key=lambda r: r[1])
    fig, ax = plt.subplots(figsize=(8, 1.2 + 0.22 * len(rows)), facecolor="#fcfcfb")
    ax.set_facecolor("#fcfcfb")
    ys = range(len(rows))
    bars = ax.barh(
        ys,
        [r[1] / DROPS for r in rows],
        height=0.7,
        color=["#2a78d6" if r[1] > 0 else "#e34948" for r in rows],
    )
    for bar, r in zip(bars, rows, strict=True):
        if r[2]:
            bar.set_hatch("///")
            bar.set_edgecolor("#fcfcfb")
    ax.set_yticks(list(ys), [short(r[0]) + (" (new)" if r[2] else "") for r in rows], fontsize=7)
    ax.axvline(0, color="#6b6b66", linewidth=1)
    ax.grid(axis="x", color="#e6e5e1", linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.set_xlabel("balance change B - A (PFT); blue = increase, red = decrease, hatched = created in window")
    n = len(deltas(d))
    ax.set_title(
        f"Per-account balance change, ledgers {d['a']['ledger_index']}..{d['b']['ledger_index']}"
        + (f" (top {limit} of {n} by size)" if n > limit else f" ({n} accounts)"),
        fontsize=10,
        loc="left",
    )
    fig.tight_layout()
    fig.savefig(path, dpi=110, metadata={"Software": None})
    plt.close(fig)


def render(d, f, top=10, crosscheck=(), tests=None, chart_name=None):
    a, b = d["a"], d["b"]
    out = [
        "# acctchurn: account churn and funding between two ledger-state snapshots",
        "",
        f"- Snapshot A: ledger {a['ledger_index']} ({a['close_time_iso']}), {a['objects_paged']} objects,"
        f" hash `{short(a['ledger_hash'])}`",
        f"- Snapshot B: ledger {b['ledger_index']} ({b['close_time_iso']}), {b['objects_paged']} objects,"
        f" hash `{short(b['ledger_hash'])}`",
        f"- Window: {b['ledger_index'] - a['ledger_index']} ledgers",
        "",
        "## Objects by type",
        "",
        "| Type | in A | in B | created | deleted | changed | unchanged |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for t, c in sorted(d["by_type"].items()):
        out.append(
            f"| {t} | {c['in_a']} | {c['in_b']} | {c['created']} | {c['deleted']} | {c['changed']}"
            f" | {c['unchanged']} |"
        )
    n = d["native"]
    out += [
        "",
        "## Native PFT conservation (exact, in drops)",
        "",
        "| | A | B | B - A |",
        "|---|---:|---:|---:|",
    ]
    for k in ("account_root", "escrow", "paychannel", "total"):
        out.append(f"| {k} | {pft(n['a'][k])} | {pft(n['b'][k])} | {pft(n['b'][k] - n['a'][k])} |")
    out.append(
        f"| total_coins (header) | {pft(a['total_coins'])} | {pft(b['total_coins'])}"
        f" | {pft(b['total_coins'] - a['total_coins'])} |"
    )
    out += ["", f"Burned (total_coins A - B): {n['burned_drops']} drops."]
    if n.get("fee_sum_drops") is not None:
        out.append(f"Sum of fee_drops of every transaction in ledgers A+1..B: {n['fee_sum_drops']} drops.")
    out += ["", "| Check | Result |", "|---|---|"]
    for k, ok in sorted(d["checks"].items()):
        out.append(f"| {k} | {'PASS' if ok else 'FAIL'} |")

    created = f["created"]
    out += [
        "",
        "## Created and deleted accounts",
        "",
        f"- Created: {len(d['accounts']['created'])} (creating tx found for {len(created)},"
        f" not found {len(f['not_found'])})",
        f"- Deleted: {len(d['accounts']['deleted'])}",
        f"- Changed (same account, new state): {len(d['accounts']['changed'])}",
        f"- Never active in the window (no tx sent after creation): {f['never_active']} of {len(created)}",
        f"- Funding chains (a new account funded by another new account): {len(f['chains'])}",
    ]
    if created:
        mult = sorted(r["reserve_multiple"] for r in created)
        out.append(
            f"- Funded amount in base reserves: min {mult[0]}, median {mult[len(mult) // 2]}, max {mult[-1]}"
        )
    types = {}
    for r in created:
        types[r["tx_type"]] = types.get(r["tx_type"], 0) + 1
    if types:
        out.append("- Creating tx types: " + ", ".join(f"{t} {c}" for t, c in sorted(types.items())))
    out += [
        "",
        f"## Top {top} funders",
        "",
        "| Funder | Accounts | Share | PFT sent to them |",
        "|---|---:|---:|---:|",
    ]
    for r in f["funders"][:top]:
        out.append(
            f"| `{short(r['funder'])}` | {r['accounts']} | {r['share'] * 100:.1f}% | {pft(r['drops'])} |"
        )
    if d["accounts"]["deleted"]:
        out += [
            "",
            "## Deleted accounts",
            "",
            "| Account | Balance at A (PFT) | OwnerCount at A |",
            "|---|---:|---:|",
        ]
        for r in d["accounts"]["deleted"]:
            out.append(f"| `{short(r['address'])}` | {pft(r['balance_drops'])} | {r['owner_count']} |")
    bc = d["balance_changes"]
    out += [
        "",
        "## Balance changes (every AccountRoot that differs; created from 0, deleted to 0)",
        "",
        "| | PFT | accounts |",
        "|---|---:|---:|",
        f"| gross increase | {pft(bc['gross_increase_drops'])} | {bc['accounts_up']} |",
        f"| gross decrease | {pft(-bc['gross_decrease_drops'])} | {bc['accounts_down']} |",
        f"| net | {pft(bc['net_drops'])} | {bc['accounts_up'] + bc['accounts_down']} |",
        f"| changed, same balance | - | {bc['accounts_same_balance']} |",
        "",
        "Net equals the AccountRoot part of the native total, B - A"
        f" ({'PASS' if d['checks'].get('net_balance_change_eq_delta_account_root') else 'FAIL'}).",
        "",
        f"Largest {top} changes:",
        "",
        "| Account | Change (PFT) | Created in window |",
        "|---|---:|---|",
    ]
    for addr, delta, new in deltas(d)[:top]:
        out.append(f"| `{short(addr)}` | {pft(delta)} | {'yes' if new else 'no'} |")
    if chart_name:
        out += ["", f"![Per-account balance change]({chart_name})"]
    rpc = f["rpc"]
    kinds = ", ".join(f"{k} {v}" for k, v in sorted(rpc.get("retries_by_error", {}).items())) or "none"
    out += [
        "",
        "## RPC, crosschecks and tests",
        "",
        f"- funders (public RPC): {rpc['calls']} calls, {rpc['retries']} retries (by error: {kinds})",
    ]
    for line in crosscheck:
        out.append(f"- crosscheck: `{line}`")
    if tests:
        out.append(f"- tests: {tests}")
    out.append("")
    return "\n".join(out)


def summaries(path):
    with open(path) as fh:
        return [ln.strip() for ln in fh if ln.startswith("SUMMARY ")]


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("diff_json")
    p.add_argument("funders_json")
    p.add_argument("--out", required=True)
    p.add_argument("--top", type=int, default=10)
    p.add_argument("--chart")
    p.add_argument("--crosscheck", action="append", default=[])
    p.add_argument("--tests")
    args = p.parse_args(argv)
    with open(args.diff_json) as fh:
        d = json.load(fh)
    with open(args.funders_json) as fh:
        f = json.load(fh)
    lines = [ln for path in args.crosscheck for ln in summaries(path)]
    name = None
    if args.chart:
        chart(d, args.chart)
        name = os.path.relpath(args.chart, os.path.dirname(os.path.abspath(args.out)))
    with open(args.out, "w") as fh:
        fh.write(render(d, f, args.top, lines, args.tests, name))
    return 0


if __name__ == "__main__":
    sys.exit(main())
