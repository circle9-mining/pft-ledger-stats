#!/usr/bin/env python3
"""Resumable, read-only per-transaction sender collector (Python 3 stdlib only).

Forward-follows validated ledgers on your own node's admin JSON-RPC through
`docker exec <container> curl` (same client, rate limit and resume rules as
txmix/collect_tx.py), fetching each ledger once with `transactions: true, expand: true`.

Outputs (in --out):
  tx.csv        one row per transaction: ledger, close time, tx index, hash, sender account
                (full address: needed for the public-RPC crosscheck; the report truncates it),
                type, result, fee, sequence, ticket flag
  ledgers.csv   one row per fetched ledger: hash, close time, txn_count, tx_rows written
  gaps.csv      ledgers that could not be fetched after retries
  retries.csv   every failed fetch attempt (ledger, attempt, error), including ones that later worked
  restarts.csv  one row per process start: kind (start/resume), time, first ledger, lag, rows dropped
  state.json    window start/end and start seq, so a restart resumes the same window
  collect.log   (--log) start/resume, hourly progress, gaps, final summary

Write order per ledger: tx.csv rows first, then the ledgers.csv row. On resume a torn last line
is cut from each file and tx.csv rows after the last ledgers.csv/gaps.csv ledger are dropped (they
are fetched again), so a SIGKILL at any point cannot duplicate or lose a ledger. SIGTERM/SIGINT
stop cleanly after the current ledger. The window is enforced client-side: no ledger closed after
the window end time is written, whatever the node returns.
"""

import argparse
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "txmix"))
from collect_tx import (  # noqa: E402
    GAP_FIELDS,
    RIPPLE_EPOCH,
    Rpc,
    append_rows,
    count_rows,
    drop_orphan_tx,
    drop_torn_tail,
    last_index,
    log,
    ripple_iso,
    utcnow,
)

TX_FIELDS = [
    "ledger_index", "close_time", "close_time_iso", "tx_index", "tx_hash", "account",
    "tx_type", "result", "fee_drops", "sequence", "ticket",
]
LEDGER_FIELDS = ["ledger_index", "ledger_hash", "close_time", "close_time_iso", "txn_count", "tx_rows",
                 "fetched_utc"]
RETRY_FIELDS = ["ledger_index", "attempt", "error", "logged_at"]
RESTART_FIELDS = ["kind", "at_utc", "first_ledger", "validated", "lag", "torn_bytes", "orphan_rows"]

_stop = False


def sender_rows(ledger):
    """Per-transaction rows of one expanded ledger, sorted by TransactionIndex."""
    idx = int(ledger["ledger_index"])
    ct = int(ledger["close_time"])
    rows = []
    for tx in ledger.get("transactions") or []:
        meta = tx.get("metaData") or tx.get("meta") or {}
        if "TransactionIndex" not in meta:
            raise ValueError(f"ledger {idx}: tx {tx.get('hash', '?')} has no TransactionIndex")
        rows.append({
            "ledger_index": idx,
            "close_time": ct,
            "close_time_iso": ripple_iso(ct),
            "tx_index": int(meta["TransactionIndex"]),
            "tx_hash": tx.get("hash", ""),
            "account": tx.get("Account", ""),
            "tx_type": tx.get("TransactionType", ""),
            "result": meta.get("TransactionResult", ""),
            "fee_drops": int(tx.get("Fee", 0)),
            "sequence": int(tx.get("Sequence", 0)),
            "ticket": "y" if "TicketSequence" in tx else "n",
        })
    rows.sort(key=lambda r: r["tx_index"])
    return rows


def in_window(close_time, end_unix):
    """Client-side window rule: a ledger belongs to the window iff it closed at or before the end."""
    return int(close_time) + RIPPLE_EPOCH <= end_unix


def _handle(signum, _frame):
    global _stop
    _stop = True


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="data/senders/tx")
    p.add_argument("--log", default=None, help="append log lines to this file too")
    p.add_argument("--container", default="postfiatd")
    p.add_argument("--port", type=int, default=5005)
    p.add_argument("--rate", type=float, default=1.0, help="max requests/second (default 1, max 1)")
    p.add_argument("--hours", type=float, default=72.0, help="window length from first start")
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

    tx_csv = os.path.join(a.out, "tx.csv")
    ledgers_csv = os.path.join(a.out, "ledgers.csv")
    gaps_csv = os.path.join(a.out, "gaps.csv")
    retries_csv = os.path.join(a.out, "retries.csv")
    restarts_csv = os.path.join(a.out, "restarts.csv")
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
    torn_total = orphans = 0
    if state:
        for path in (tx_csv, ledgers_csv, gaps_csv, retries_csv, restarts_csv):
            torn = drop_torn_tail(path)
            torn_total += torn
            if torn:
                log(lf, f"RESUME dropped torn last line of {os.path.basename(path)} ({torn} bytes)")
        done = max(
            x
            for x in (last_index(ledgers_csv), last_index(gaps_csv), state["start_seq"] - 1)
            if x is not None
        )
        orphans = drop_orphan_tx(tx_csv, done)
        if orphans:
            log(lf, f"RESUME dropped {orphans} tx.csv rows after ledger {done} (no ledgers.csv row)")
        nxt = done + 1
        kind = "resume"
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
        kind = "start"
        log(
            lf,
            f"WINDOW START {state['start_utc']} -> {state['end_utc']} at ledger {nxt}, "
            f"stop_at {a.stop_at}, rate <= {a.rate} req/s",
        )
    append_rows(restarts_csv, RESTART_FIELDS, [{
        "kind": kind, "at_utc": utcnow(), "first_ledger": nxt, "validated": validated,
        "lag": validated - nxt + 1, "torn_bytes": torn_total, "orphan_rows": orphans,
    }])
    stop_at = state.get("stop_at")

    reason = None
    next_progress = time.monotonic() + 3600
    while not _stop and (stop_at is None or nxt <= stop_at):
        if time.monotonic() >= next_progress:
            log(
                lf,
                f"PROGRESS last={nxt - 1} lag={validated - nxt + 1} ledgers={count_rows(ledgers_csv)} "
                f"tx={count_rows(tx_csv)} gaps={count_rows(gaps_csv)} retries={count_rows(retries_csv)} "
                f"requests={rpc.count}",
            )
            next_progress = time.monotonic() + 3600
        if nxt > validated:
            if time.time() > state["end"] + 30:
                reason = "window end reached"
                break
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
                ct = int(led["close_time"])
                if not in_window(ct, state["end"]):
                    reason = f"window end reached (ledger {nxt} closed {ripple_iso(ct)}, not written)"
                    break
                rows = sender_rows(led)
                append_rows(tx_csv, TX_FIELDS, rows)
                append_rows(ledgers_csv, LEDGER_FIELDS, [{
                    "ledger_index": nxt, "ledger_hash": led["ledger_hash"], "close_time": ct,
                    "close_time_iso": ripple_iso(ct), "txn_count": len(led.get("transactions") or []),
                    "tx_rows": len(rows), "fetched_utc": utcnow(),
                }])
                err = None
                break
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                append_rows(retries_csv, RETRY_FIELDS, [{
                    "ledger_index": nxt, "attempt": attempt, "error": err, "logged_at": utcnow(),
                }])
                log(lf, f"WARN ledger {nxt} attempt {attempt}/{a.retries}: {err}")
                time.sleep(2.0)
        if reason:
            break
        if err:
            append_rows(gaps_csv, GAP_FIELDS, [{"ledger_index": nxt, "error": err, "logged_at": utcnow()}])
            log(lf, f"GAP ledger {nxt}: {err}")
        nxt += 1

    if reason is None:
        reason = "signal" if _stop else "stop_at reached"
    log(lf, f"STOP ({reason}) requests this run={rpc.count}")
    log(
        lf,
        f"SUMMARY window {state['start_utc']} -> {state['end_utc']} "
        f"ledgers {state['start_seq']}-{nxt - 1} fetched={count_rows(ledgers_csv)} "
        f"tx={count_rows(tx_csv)} gaps={count_rows(gaps_csv)} retries={count_rows(retries_csv)} "
        f"restarts={count_rows(restarts_csv)}",
    )
    if lf:
        lf.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
