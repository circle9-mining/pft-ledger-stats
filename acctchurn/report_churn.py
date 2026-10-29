#!/usr/bin/env python3
"""Write REPORT.md from diff.json (diff_churn.py) and funders.json (funders_churn.py).

Offline and deterministic: the same inputs give byte-identical output. Addresses and hashes
are truncated to 8 characters; drops are shown as PFT with 6 decimals.

Usage:
  ./report_churn.py diff.json funders.json --out acctchurn/REPORT.md [--top 10]
"""

import argparse
import json
import sys

DROPS = 1_000_000


def short(a):
    return (a or "?")[:8] + "…"


def pft(drops):
    sign = "-" if drops < 0 else ""
    q, r = divmod(abs(drops), DROPS)
    return f"{sign}{q:,}.{r:06d}"


def render(d, f, top=10):
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
        f"- Changed (same account, new state): {d['accounts']['changed']}",
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
    out += ["", f"RPC: {f['rpc']['calls']} calls, {f['rpc']['retries']} retries.", ""]
    return "\n".join(out)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("diff_json")
    p.add_argument("funders_json")
    p.add_argument("--out", required=True)
    p.add_argument("--top", type=int, default=10)
    args = p.parse_args(argv)
    with open(args.diff_json) as fh:
        d = json.load(fh)
    with open(args.funders_json) as fh:
        f = json.load(fh)
    with open(args.out, "w") as fh:
        fh.write(render(d, f, args.top))
    return 0


if __name__ == "__main__":
    sys.exit(main())
