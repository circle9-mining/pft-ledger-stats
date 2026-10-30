#!/usr/bin/env python3
"""Resumable, read-only signing-key transaction collector with full metadata (Python 3 stdlib only).

Forward-follows validated ledgers on your own node's admin JSON-RPC through
`docker exec <container> curl` (same client, rate limit and resume rules as
nftflow/collect_nft.py), fetching each ledger once with `transactions: true, expand: true`.

A transaction is "key-relevant" (written to key-tx.jsonl) if any of these is true:
  - its type is SetRegularKey, SignerListSet or AccountSet (all results, also tec failures)
  - its metadata touches a SignerList node (created, modified or deleted)
  - its metadata creates or deletes an AccountRoot (new accounts start master-key-only,
    deleted accounts leave the population; both are needed for exact A->B reconciliation)
  - its metadata modifies an AccountRoot whose RegularKey changed or whose lsfDisableMaster
    flag bit differs between PreviousFields and FinalFields

Outputs (in --out):
  key-tx.jsonl  one JSON line per key-relevant tx: ledger_index, close_time, tx_index,
                the full tx JSON and its full metadata
  ledgers.csv   one row per fetched ledger: hash, close time, txn_count (all txs),
                key_tx_count (lines written to key-tx.jsonl for that ledger)
  gaps.csv      ledgers that could not be fetched after retries
  state.json    window start/end and start seq, so a restart resumes the window
  collect.log   (--log) start/resume, hourly progress, gaps, final summary

Write order per ledger: key-tx.jsonl lines first, then the ledgers.csv row. On resume a torn
last line is cut from each file and key-tx.jsonl lines after the last ledgers.csv/gaps.csv
ledger are dropped (they are fetched again), so a SIGKILL at any point cannot duplicate or lose
a ledger. SIGTERM/SIGINT stop cleanly after the current ledger.
"""

import argparse
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "txmix"))
sys.path.insert(0, os.path.join(_HERE, "..", "nftflow"))
from collect_nft import count_lines, drop_orphan_lines  # noqa: E402
from collect_tx import (  # noqa: E402
    GAP_FIELDS,
    Rpc,
    append_rows,
    count_rows,
    drop_torn_tail,
    last_index,
    log,
    ripple_iso,
    utcnow,
)

LEDGER_FIELDS = [
    "ledger_index",
    "ledger_hash",
    "close_time",
    "close_time_iso",
    "txn_count",
    "key_tx_count",
    "fetched_utc",
]
KEY_TX_TYPES = {"SetRegularKey", "SignerListSet", "AccountSet"}
LSF_DISABLE_MASTER = 0x00100000

_stop = False


def key_reasons(tx, meta):
    """Why a tx is key-relevant (empty list = not relevant)."""
    why = []
    if tx.get("TransactionType") in KEY_TX_TYPES:
        why.append("type")
    for node in meta.get("AffectedNodes", []):
        for kind, v in node.items():
            let = v.get("LedgerEntryType")
            if let == "SignerList":
                why.append("signerlist")
            elif let == "AccountRoot" and kind in ("CreatedNode", "DeletedNode"):
                why.append("account_" + kind[:7].lower())
            elif let == "AccountRoot" and kind == "ModifiedNode":
                prev = v.get("PreviousFields") or {}
                fin = v.get("FinalFields") or {}
                if "RegularKey" in prev:
                    why.append("regularkey")
                if "Flags" in prev and (int(prev["Flags"]) ^ int(fin.get("Flags", 0))) & LSF_DISABLE_MASTER:
                    why.append("disablemaster")
    return sorted(set(why))


def key_lines(ledger):
    """Key-relevant txs of one expanded ledger, sorted by TransactionIndex."""
    idx = int(ledger["ledger_index"])
    ct = int(ledger["close_time"])
    out = []
    for tx in ledger.get("transactions") or []:
        meta = tx.get("metaData") or tx.get("meta") or {}
        t = {k: v for k, v in tx.items() if k not in ("metaData", "meta")}
        why = key_reasons(t, meta)
        if not why:
            continue
        out.append(
            {
                "ledger_index": idx,
                "close_time": ct,
                "close_time_iso": ripple_iso(ct),
                "tx_index": meta.get("TransactionIndex", 0),
                "why": why,
                "tx": t,
                "meta": meta,
            }
        )
    out.sort(key=lambda r: r["tx_index"])
    return out, len(ledger.get("transactions") or [])


def _handle(signum, _frame):
    global _stop
    _stop = True


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="data/keyposture/tx")
    p.add_argument("--log", default=None, help="append log lines to this file too")
    p.add_argument("--container", default="postfiatd")
    p.add_argument("--port", type=int, default=5005)
    p.add_argument("--rate", type=float, default=1.0, help="max requests/second (default 1, max 1)")
    p.add_argument("--hours", type=float, default=24.0, help="window length from first start")
    p.add_argument("--start", type=int, default=None, help="first ledger (default: current validated)")
    p.add_argument("--stop-at", type=int, default=None, help="last ledger to fetch, then exit")
    p.add_argument("--retries", type=int, default=3)
    p.add_argument("--no-sudo", action="store_true", help="call docker without sudo")
    a = p.parse_args(argv)

    if a.rate > 1.0:
        p.error("--rate above 1 req/s is not allowed")
    os.makedirs(a.out, exist_ok=True)
    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)
    lf = open(a.log, "a") if a.log else None
    rpc = Rpc(a.container, a.port, a.rate, ["docker"] if a.no_sudo else ["sudo", "docker"])

    jl = os.path.join(a.out, "key-tx.jsonl")
    ledgers_csv = os.path.join(a.out, "ledgers.csv")
    gaps_csv = os.path.join(a.out, "gaps.csv")
    state_path = os.path.join(a.out, "state.json")

    info = rpc.call("server_info")["info"]
    log(
        lf,
        f"node: build {info.get('build_version')} state {info.get('server_state')} "
        f"complete_ledgers {info.get('complete_ledgers')}",
    )
    validated = int(info["validated_ledger"]["seq"])

    state = {}
    if os.path.exists(state_path):
        with open(state_path) as fh:
            state = json.load(fh)
    if state:
        for path in (jl, ledgers_csv, gaps_csv):
            torn = drop_torn_tail(path)
            if torn:
                log(lf, f"RESUME dropped torn last line of {os.path.basename(path)} ({torn} bytes)")
        done = max(
            x
            for x in (last_index(ledgers_csv), last_index(gaps_csv), state["start_seq"] - 1)
            if x is not None
        )
        orphans = drop_orphan_lines(jl, done)
        if orphans:
            log(lf, f"RESUME dropped {orphans} key-tx.jsonl lines after ledger {done} (no ledgers.csv row)")
        nxt = done + 1
        log(
            lf,
            f"RESUME at ledger {nxt} (window {state['start_utc']} -> {state['end_utc']}, "
            f"start seq {state['start_seq']}, behind by {validated - nxt + 1})",
        )
    else:
        nxt = a.start if a.start is not None else validated
        now = time.time()
        state = {
            "start": int(now),
            "end": int(now + a.hours * 3600),
            "start_seq": nxt,
            "start_utc": utcnow(),
            "stop_at": a.stop_at,
            "end_utc": datetime.fromtimestamp(now + a.hours * 3600, timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
        }
        with open(state_path, "w") as fh:
            json.dump(state, fh, indent=1)
        log(
            lf,
            f"WINDOW START {state['start_utc']} -> {state['end_utc']} at ledger {nxt}, "
            f"stop_at {a.stop_at}, rate <= {a.rate} req/s",
        )
    stop_at = state.get("stop_at")

    next_progress = time.monotonic() + 3600
    while not _stop and time.time() < state["end"] and (stop_at is None or nxt <= stop_at):
        if time.monotonic() >= next_progress:
            log(
                lf,
                f"PROGRESS last={nxt - 1} lag={validated - nxt + 1} ledgers={count_rows(ledgers_csv)} "
                f"key_tx={count_lines(jl)} gaps={count_rows(gaps_csv)} requests={rpc.count}",
            )
            next_progress = time.monotonic() + 3600
        if nxt > validated:
            try:
                validated = int(rpc.call("server_info")["info"]["validated_ledger"]["seq"])
            except Exception as e:
                log(lf, f"WARN server_info failed: {e}")
            if nxt > validated:
                time.sleep(1.0)
            continue
        err = None
        for attempt in range(1, a.retries + 1):
            try:
                led = rpc.call("ledger", {"ledger_index": nxt, "transactions": True, "expand": True})[
                    "ledger"
                ]
                if not led.get("closed", True) or int(led["ledger_index"]) != nxt:
                    raise LookupError(f"unexpected ledger {led.get('ledger_index')}")
                lines, n_all = key_lines(led)
                with open(jl, "a") as fh:
                    for x in lines:
                        fh.write(json.dumps(x, sort_keys=True, separators=(",", ":")) + "\n")
                ct = int(led["close_time"])
                append_rows(
                    ledgers_csv,
                    LEDGER_FIELDS,
                    [
                        {
                            "ledger_index": nxt,
                            "ledger_hash": led["ledger_hash"],
                            "close_time": ct,
                            "close_time_iso": ripple_iso(ct),
                            "txn_count": n_all,
                            "key_tx_count": len(lines),
                            "fetched_utc": utcnow(),
                        }
                    ],
                )
                err = None
                break
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                log(lf, f"WARN ledger {nxt} attempt {attempt}/{a.retries}: {err}")
                time.sleep(2.0)
        if err:
            append_rows(gaps_csv, GAP_FIELDS, [{"ledger_index": nxt, "error": err, "logged_at": utcnow()}])
            log(lf, f"GAP ledger {nxt}: {err}")
        nxt += 1

    reason = (
        "signal"
        if _stop
        else ("stop_at reached" if stop_at is not None and nxt > stop_at else "window end reached")
    )
    log(lf, f"STOP ({reason}) requests this run={rpc.count}")
    log(
        lf,
        f"SUMMARY window {state['start_utc']} -> {state['end_utc']} "
        f"ledgers {state['start_seq']}-{nxt - 1} fetched={count_rows(ledgers_csv)} "
        f"key_tx={count_lines(jl)} gaps={count_rows(gaps_csv)}",
    )
    if lf:
        lf.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
