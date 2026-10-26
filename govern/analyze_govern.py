#!/usr/bin/env python3
"""Per-validator amendment and fee-vote analysis of a govern.jsonl capture (collect_govern.py).

Read-only, offline. Inputs: govern.jsonl, the collector's state.json (window), and UNL snapshots taken
with unl_snapshot.sh at window start (and end). Output: REPORT.md, summary.json, matrix.csv.

Definitions (all from this node's single vantage point):
  voting ledger   li with (li + 1) % 256 == 0; validators put amendment/fee votes in validations of it.
  flag ledger     li % 256 == 0; the vote on voting ledger F-1 is tallied when F closes and its
                  EnableAmendment / SetFee pseudo-transactions land in F+1, so they show in the
                  Amendments / FeeSettings snapshot taken at the NEXT flag ledger F+256.
  validator       keyed by master key (validation master_key, else the UNL signing-key map, else the
                  signing key itself, marked `sk:`).
  yes-share       (voting ledgers on which the validator voted for the amendment) / (voting ledgers it
                  validated); reported over the whole window and over its active span (first..last
                  voting ledger it validated here).
  UNL yes-share   (UNL validators voting yes on a voting ledger) / (UNL size); 80% is the threshold
                  for a Majority.
  missing vote    a voting ledger inside the validator's active span (UNL members: the whole window)
                  that this node delivered but saw no validation from that validator for.
  build-blocked   risk flag: the validator's server_version is older than the newest build seen from a
                  UNL validator. An amendment that only the newer build supports would leave it
                  amendment-blocked once enabled.
"""

import argparse
import csv
import json
import os
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

THRESHOLD = 0.8
RIPPLE_EPOCH = 946684800
MAJORITY_PERIOD_S = 14 * 86400  # XRPL default; not readable over RPC, so the projection is labelled


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if t is not None else "-"


def decode_sv(sv):
    """server_version (uint64 as string) -> 'major.minor.patch' for the 0x183B implementer, else raw."""
    try:
        v = int(sv)
    except (TypeError, ValueError):
        return None
    if v >> 48 != 0x183B:
        return str(v)
    rel = {3: "", 2: "-rc", 1: "-b"}.get((v >> 22) & 3, "-?")
    return f"{(v >> 40) & 0xFF}.{(v >> 32) & 0xFF}.{(v >> 24) & 0xFF}{rel}"


def sv_key(sv):
    try:
        return int(sv) >> 16  # implementer + major/minor/patch + release type, ignores reserved bits
    except (TypeError, ValueError):
        return -1


def load(path):
    evs, bad = [], 0
    with open(path) as fh:
        for line in fh:
            try:
                evs.append(json.loads(line))
            except ValueError:
                bad += 1
    return evs, bad


def load_unl(path):
    if not path or not os.path.exists(path):
        return None
    with open(path) as fh:
        u = json.load(fh)
    return {
        "t": u.get("t"),
        "quorum": u.get("validation_quorum"),
        "keys": set(u.get("trusted_validator_keys") or []),
        "signing": {sk: mk for mk, sk in (u.get("signing_keys") or {}).items()},
        "seq": [p.get("seq") for p in u.get("publisher_lists") or []],
    }


def analyze(evs, unl, unl_end=None, window=None):
    ledgers = sorted({e["li"] for e in evs if e.get("ev") == "ledger"})
    gaps = [e for e in evs if e.get("ev") == "gap"]
    sessions = [e for e in evs if e.get("ev") == "session"]
    lo, hi = (ledgers[0], ledgers[-1]) if ledgers else (None, None)
    lset = set(ledgers)
    exp_flags = [li for li in range(lo, hi + 1) if li % 256 == 0] if ledgers else []
    exp_voting = [li for li in range(lo, hi + 1) if (li + 1) % 256 == 0] if ledgers else []
    seen_flags = [li for li in exp_flags if li in lset]
    seen_voting = [li for li in exp_voting if li in lset]
    signing = (unl or {}).get("signing", {})
    unl_keys = (unl or {}).get("keys", set())

    def vid(e):
        if e.get("mk"):
            return e["mk"]
        return signing.get(e["k"]) or "sk:" + e["k"]

    # one vote per (validator, voting ledger): prefer the validation for the ledger hash that this node
    # saw validated; count extra hashes (validations of a losing ledger) separately
    led_hash = {e["li"]: e.get("lh") for e in evs if e.get("ev") == "ledger"}
    votes, other_hash, partial = {}, 0, 0
    for e in evs:
        if e.get("ev") != "vote":
            continue
        if not e.get("full", True):
            partial += 1
        key = (vid(e), e["li"])
        if led_hash.get(e["li"]) not in (None, e["lh"]):
            other_hash += 1
            continue
        votes[key] = e
    per = defaultdict(dict)
    for (v, li), e in votes.items():
        per[v][li] = e

    newest = max((sv_key(e.get("sv")) for (v, _), e in votes.items() if v in unl_keys), default=-1)
    amendments = sorted({a for e in votes.values() for a in e.get("am") or []})
    rows = []
    for v in sorted(per, key=lambda x: (x not in unl_keys, x)):
        vl = per[v]
        lis = sorted(vl)
        span = [li for li in seen_voting if lis[0] <= li <= lis[-1]]
        expect = seen_voting if v in unl_keys else span
        missing = [li for li in expect if li not in vl]
        changes, prev = [], None
        for li in lis:
            cur = set(vl[li].get("am") or [])
            if prev is not None and cur != prev:
                changes.append({"li": li, "added": sorted(cur - prev), "removed": sorted(prev - cur)})
            prev = cur
        svs = Counter(vl[li].get("sv") for li in lis)
        sv = svs.most_common(1)[0][0]
        yes = Counter(a for li in lis for a in vl[li].get("am") or [])
        fees = Counter(json.dumps(vl[li].get("fee") or {}, sort_keys=True) for li in lis)
        rows.append(
            {
                "validator": v,
                "unl": v in unl_keys,
                "signing_keys": sorted({vl[li]["k"] for li in lis}),
                "voting_ledgers_validated": len(lis),
                "active_span": [lis[0], lis[-1]],
                "span_voting_ledgers": len(span),
                "missing_votes": missing,
                "yes_share_window": {a: yes[a] / len(seen_voting) for a in yes} if seen_voting else {},
                "yes_share_span": {a: yes[a] / len(span) for a in yes} if span else {},
                "amendments_voted": sorted(yes),
                "vote_changes": changes,
                "fee_votes": {k: n for k, n in fees.items() if k != "{}"},
                "server_version": decode_sv(sv),
                "server_versions_seen": sorted({decode_sv(s) or "-" for s in svs}),
                "build_blocked_risk": 0 <= sv_key(sv) < newest,
                "extra_fields": sorted({f for li in lis for f in vl[li].get("extra") or []}),
            }
        )

    # UNL tally per voting ledger
    tally = []
    n_unl = len(unl_keys)
    for li in seen_voting:
        voters = [v for v in unl_keys if (v, li) in votes]
        c = Counter(a for v in voters for a in votes[(v, li)].get("am") or [])
        tally.append(
            {
                "li": li,
                "unl_validations": len(voters),
                "yes": dict(c),
                "share": {a: c[a] / n_unl for a in c} if n_unl else {},
                "over_threshold": sorted(a for a in c if n_unl and c[a] / n_unl >= THRESHOLD),
            }
        )

    # Amendments / FeeSettings snapshots
    snaps = defaultdict(dict)
    snap_errors = []
    for e in evs:
        if e.get("ev") == "snap":
            if "node" in e:
                snaps[e["li"]][e["obj"]] = e["node"]
            else:
                snap_errors.append({"li": e["li"], "obj": e["obj"], "error": e.get("error")})
    maj_hist, events_maj, prev = [], [], None
    enabled_first = enabled_last = None
    for li in sorted(snaps):
        a = snaps[li].get("Amendments")
        if a is None:
            continue
        enabled = a.get("Amendments") or []
        enabled_first = enabled_first if enabled_first is not None else (li, len(enabled))
        enabled_last = (li, len(enabled))
        majs = {m["Majority"]["Amendment"]: m["Majority"]["CloseTime"] for m in a.get("Majorities") or []}
        maj_hist.append({"li": li, "majorities": len(majs)})
        cur = set(majs)
        if prev is not None:
            for x in sorted(cur - prev):
                ct = majs[x] + RIPPLE_EPOCH
                events_maj.append(
                    {
                        "li": li,
                        "event": "appeared",
                        "amendment": x,
                        "since": iso(ct),
                        "projected_activation": iso(ct + MAJORITY_PERIOD_S),
                    }
                )
            for x in sorted(prev - cur):
                events_maj.append({"li": li, "event": "disappeared", "amendment": x})
        prev = cur
    current_maj = {}
    if maj_hist:
        a = snaps[maj_hist[-1]["li"]]["Amendments"]
        for m in a.get("Majorities") or []:
            ct = m["Majority"]["CloseTime"] + RIPPLE_EPOCH
            current_maj[m["Majority"]["Amendment"]] = {
                "since": iso(ct),
                "projected_activation": iso(ct + MAJORITY_PERIOD_S),
            }
    fee_states = []
    for li in sorted(snaps):
        f = snaps[li].get("FeeSettings")
        if f is not None:
            cur = {
                k: f[k]
                for k in sorted(f)
                if k not in ("index", "LedgerEntryType", "Flags", "PreviousTxnID", "PreviousTxnLgrSeq")
            }
            if not fee_states or fee_states[-1]["fields"] != cur:
                fee_states.append({"li": li, "fields": cur})

    fee_vote_rows = [r for r in rows if r["fee_votes"]]
    unl_seen = [r for r in rows if r["unl"]]
    return {
        "window": [iso(window["window_start"]), iso(window["window_end"])] if window else None,
        "ledger_range": [lo, hi],
        "ledgers": len(ledgers),
        "gaps": [{k: g[k] for k in ("from", "to", "count", "reason")} for g in gaps],
        "sessions": len(sessions),
        "flag_ledgers_expected": len(exp_flags),
        "flag_ledgers_seen": len(seen_flags),
        "voting_ledgers_expected": len(exp_voting),
        "voting_ledgers_seen": len(seen_voting),
        "vote_entries": sum(1 for e in evs if e.get("ev") == "vote"),
        "vote_entries_other_hash": other_hash,
        "partial_validations": partial,
        "validators": len(rows),
        "unl_size": n_unl,
        "unl_quorum": (unl or {}).get("quorum"),
        "unl_seen": len(unl_seen),
        "unl_snapshot_start": (unl or {}).get("t"),
        "unl_snapshot_end": (unl_end or {}).get("t"),
        "unl_changed": bool(unl_end) and unl_end["keys"] != unl_keys,
        "amendments_voted": amendments,
        "amendments_enabled": {"first": enabled_first, "last": enabled_last},
        "majorities_history": maj_hist,
        "majority_events": events_maj,
        "majorities_current": current_maj,
        "unl_tally": tally,
        "fee_settings": fee_states,
        "fee_voters": len(fee_vote_rows),
        "snap_errors": snap_errors,
        "newest_unl_build": max(
            (r["server_version"] for r in unl_seen),
            key=lambda s: tuple(map(int, s.split(".")[:3])) if s and s.replace(".", "").isdigit() else (-1,),
            default=None,
        ),
        "per_validator": rows,
    }


def host_header():
    try:
        with open("/etc/os-release") as fh:
            osr = dict(line.rstrip("\n").split("=", 1) for line in fh if "=" in line)
        os_name = osr.get("PRETTY_NAME", "?").strip('"')
    except OSError:
        os_name = "?"
    try:
        ntp = subprocess.run(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        ntp = "?"
    return os_name, ntp


def pct(x):
    return f"{100 * x:.1f}%" if x is not None else "-"


def short(k, n=12):
    return k[:n] + "…" if k and len(k) > n else k


def render(s, os_name, ntp, title="govern report"):
    en0, en1 = (
        (x[1] if x else "-") for x in (s["amendments_enabled"]["first"], s["amendments_enabled"]["last"])
    )
    L = [
        f"# {title}",
        "",
        f"- OS: {os_name}; NTP synchronized: {ntp}",
        f"- Window (UTC): {s['window'][0]} .. {s['window'][1]}" if s["window"] else "- Window: -",
        f"- Ledgers {s['ledgers']} ({s['ledger_range'][0]}-{s['ledger_range'][1]}), collector sessions"
        f" {s['sessions']}, gaps {len(s['gaps'])} ({sum(g['count'] for g in s['gaps'])} ledgers)",
        f"- Flag ledgers seen / expected: {s['flag_ledgers_seen']} / {s['flag_ledgers_expected']};"
        f" voting ledgers seen / expected: {s['voting_ledgers_seen']} / {s['voting_ledgers_expected']}",
        f"- Validators (master keys) with a validation on a voting ledger: {s['validators']};"
        f" UNL size {s['unl_size']} (quorum {s['unl_quorum']}), UNL members seen {s['unl_seen']};"
        f" UNL changed during window: {s['unl_changed']}",
        f"- Vote entries {s['vote_entries']} (for a non-validated hash: {s['vote_entries_other_hash']};"
        f" partial: {s['partial_validations']}); amendments voted for by anyone:"
        f" {len(s['amendments_voted'])};"
        f" validators sending fee votes: {s['fee_voters']}",
        "",
        "Single vantage point: a vote counts only if this node received the validation.",
        "",
        "## Network",
        "",
        f"- Enabled amendments: {en0} at the first flag ledger, {en1} at the last",
        f"- Majorities at the last flag ledger: {len(s['majorities_current'])};"
        f" appear/disappear events in window: {len(s['majority_events'])}",
    ]
    for a, m in s["majorities_current"].items():
        L.append(
            f"  - `{a}` majority since {m['since']}, projected activation {m['projected_activation']}"
            " (assumes the XRPL default 14-day majority period)"
        )
    for ev in s["majority_events"]:
        L.append(f"  - ledger {ev['li']}: `{ev['amendment']}` {ev['event']}")
    over = [t for t in s["unl_tally"] if t["over_threshold"]]
    L += [
        f"- UNL yes-share >= {pct(THRESHOLD)} on {len(over)} of {len(s['unl_tally'])} voting ledgers",
        "- FeeSettings states in window: "
        + "; ".join(
            f"from {f['li']}: " + ", ".join(f"{k}={v}" for k, v in f["fields"].items())
            for f in s["fee_settings"]
        ),
        "",
        "## UNL tally per voting ledger",
        "",
        "| voting ledger | UNL validations | amendments with yes votes | max UNL yes-share | >= 80% |",
        "|---:|---:|---:|---:|---|",
    ]
    for t in s["unl_tally"]:
        mx = max(t["share"].values(), default=0.0)
        L.append(
            f"| {t['li']} | {t['unl_validations']}/{s['unl_size']} | {len(t['yes'])} | {pct(mx)} |"
            f" {', '.join(short(a) for a in t['over_threshold']) or '-'} |"
        )
    L += [
        "",
        "## Per-validator vote matrix",
        "",
        "`voted` = voting ledgers validated here; `span` = voting ledgers in its active span;"
        " `yes` = amendments it voted for at least once (yes-share over the window / its span);"
        " `fee` = fee-vote values;"
        " `missing` = voting ledgers in span (UNL: whole window) without its validation.",
        "",
        "| validator | UNL | build | voted | span | missing | yes (window / span) | changes | fee | flags |",
        "|---|---|---|---:|---:|---:|---|---:|---|---|",
    ]
    for r in s["per_validator"]:
        yes = (
            ", ".join(
                f"{short(a, 8)} {pct(r['yes_share_window'][a])}/{pct(r['yes_share_span'].get(a))}"
                for a in r["amendments_voted"]
            )
            or "none"
        )
        flags = [
            f
            for f, on in (
                ("build-blocked-risk", r["build_blocked_risk"]),
                ("missing-vote", bool(r["missing_votes"])),
                ("new-fields", bool(r["extra_fields"])),
            )
            if on
        ]
        fee = "; ".join(f"{k} x{n}" for k, n in r["fee_votes"].items()) or "-"
        L.append(
            f"| `{short(r['validator'], 16)}` | {'yes' if r['unl'] else 'no'} | {r['server_version']} |"
            f" {r['voting_ledgers_validated']} | {r['span_voting_ledgers']} | {len(r['missing_votes'])} |"
            f" {yes} | {len(r['vote_changes'])} | {fee} | {', '.join(flags) or '-'} |"
        )
    ch = [(r["validator"], c) for r in s["per_validator"] for c in r["vote_changes"]]
    L += ["", "## Vote changes", ""]
    L += [f"- `{short(v, 16)}` at {c['li']}: +{c['added']} -{c['removed']}" for v, c in ch] or ["none"]
    if s["gaps"]:
        L += ["", "## Gaps", "", "| from | to | count | reason |", "|---:|---:|---:|---|"]
        L += [f"| {g['from']} | {g['to']} | {g['count']} | {g['reason']} |" for g in s["gaps"]]
    if s["snap_errors"]:
        L += ["", "## Snapshot errors", ""] + [
            f"- {x['li']} {x['obj']}: {x['error']}" for x in s["snap_errors"]
        ]
    return "\n".join(L) + "\n"


SURFACE, INK, INK2, MUTED, GRID, BASE = "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7"
SERIES = ["#2a78d6", "#eb6834", "#1baf7a"]  # reference palette slots 1-3 (validated all-pairs), fixed order


def _axes(ax, grid="y"):
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(BASE)
    ax.tick_params(colors=MUTED, labelsize=9)
    ax.grid(axis=grid, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def charts(s, outdir):
    """yes-share.png: UNL yes-share per voting ledger (top 3 amendments by peak share + 80% line), and
    per-validator share of voting ledgers validated (UNL vs non-UNL). Static PNG for the Markdown report;
    the report tables carry the same numbers."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    rows = s["per_validator"]
    n_seen = max(s["voting_ledgers_seen"], 1)
    fig, (a1, a2) = plt.subplots(
        2, 1, figsize=(9, 3.6 + 0.16 * len(rows)), gridspec_kw={"height_ratios": [3, 0.16 * len(rows) + 0.6]}
    )
    fig.patch.set_facecolor(SURFACE)
    _axes(a1)
    x = [t["li"] for t in s["unl_tally"]]
    peak = {}
    for t in s["unl_tally"]:
        for a, v in t["share"].items():
            peak[a] = max(peak.get(a, 0), v)
    top = sorted(peak, key=lambda a: (-peak[a], a))[:3]
    if top:
        for i, a in enumerate(top):
            a1.plot(
                x,
                [t["share"].get(a, 0) for t in s["unl_tally"]],
                color=SERIES[i],
                linewidth=2,
                marker="o",
                markersize=4,
                label=short(a, 10),
            )
        a1.legend(frameon=False, fontsize=8, labelcolor=INK2, loc="upper left")
    else:
        a1.plot(x, [0] * len(x), color=SERIES[0], linewidth=2, marker="o", markersize=4)
        a1.text(
            0.5,
            0.45,
            "no UNL validator voted for any amendment in this window",
            transform=a1.transAxes,
            ha="center",
            color=INK2,
            fontsize=9,
        )
    a1.axhline(THRESHOLD, color=MUTED, linewidth=1, linestyle="--")
    a1.text(x[0] if x else 0, THRESHOLD + 0.02, "80% majority threshold", color=MUTED, fontsize=8)
    if x:
        a1.set_xlim(x[0] - 128, x[-1] + 128)
    a1.set_ylim(0, 1.05)
    a1.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0))
    a1.ticklabel_format(axis="x", style="plain", useOffset=False)
    a1.set_title("UNL yes-share per voting ledger", loc="left", color=INK, fontsize=11)
    a1.set_xlabel("voting ledger index", color=INK2, fontsize=9)

    _axes(a2, grid="x")
    order = sorted(rows, key=lambda r: (not r["unl"], -r["voting_ledgers_validated"], r["validator"]))
    ys = list(range(len(order)))[::-1]
    for y, r in zip(ys, order, strict=True):
        a2.barh(
            y,
            r["voting_ledgers_validated"] / n_seen,
            height=0.7,
            color=SERIES[0] if r["unl"] else SERIES[1],
            edgecolor=SURFACE,
            linewidth=1,
        )
    a2.set_yticks(ys)
    a2.set_yticklabels([short(r["validator"], 10) for r in order], fontsize=6, color=INK2)
    a2.set_xlim(0, 1)
    a2.xaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0))
    a2.set_title(
        f"Voting ledgers validated per validator (of {s['voting_ledgers_seen']} seen)",
        loc="left",
        color=INK,
        fontsize=11,
    )
    from matplotlib.patches import Patch

    a2.legend(
        handles=[Patch(color=SERIES[0], label="UNL"), Patch(color=SERIES[1], label="not on UNL")],
        frameon=False,
        fontsize=8,
        labelcolor=INK2,
        loc="lower right",
        bbox_to_anchor=(1, 1),
        ncol=2,
    )
    fig.tight_layout()
    out = os.path.join(outdir, "yes-share.png")
    fig.savefig(out, dpi=110, facecolor=SURFACE)
    plt.close(fig)
    return ["yes-share.png"]


def write_csv(s, path):
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(
            [
                "validator",
                "unl",
                "server_version",
                "voting_ledgers_validated",
                "span_voting_ledgers",
                "missing_votes",
                "amendments_voted",
                "vote_changes",
                "fee_votes",
                "build_blocked_risk",
            ]
        )
        for r in s["per_validator"]:
            w.writerow(
                [
                    r["validator"],
                    r["unl"],
                    r["server_version"],
                    r["voting_ledgers_validated"],
                    r["span_voting_ledgers"],
                    len(r["missing_votes"]),
                    " ".join(r["amendments_voted"]),
                    len(r["vote_changes"]),
                    json.dumps(r["fee_votes"], sort_keys=True),
                    r["build_blocked_risk"],
                ]
            )


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument(
        "data", help="collector --out dir (govern.jsonl, state.json, unl-start.json, unl-end.json)"
    )
    ap.add_argument("--report", required=True)
    ap.add_argument("--title", default="govern report")
    ap.add_argument("--charts", action="store_true")
    a = ap.parse_args(argv)
    evs, bad = load(os.path.join(a.data, "govern.jsonl"))
    with open(os.path.join(a.data, "state.json")) as fh:
        window = json.load(fh)
    unl = load_unl(os.path.join(a.data, "unl-start.json"))
    unl_end = load_unl(os.path.join(a.data, "unl-end.json"))
    s = analyze(evs, unl, unl_end, window)
    s["bad_lines"] = bad
    os_name, ntp = host_header()
    os.makedirs(a.report, exist_ok=True)
    with open(os.path.join(a.report, "summary.json"), "w") as fh:
        json.dump(s, fh, indent=1, sort_keys=True, default=sorted)
    write_csv(s, os.path.join(a.report, "matrix.csv"))
    md = render(s, os_name, ntp, a.title)
    if a.charts:
        md += "\n## Charts\n\n" + "".join(f"![{x}]({x})\n\n" for x in charts(s, a.report))
    with open(os.path.join(a.report, "REPORT.md"), "w") as fh:
        fh.write(md)
    print(md[:4000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
