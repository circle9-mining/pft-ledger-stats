#!/usr/bin/env python3
"""Resumable, read-only, throttled transaction collector (Python 3 stdlib only).

Forward-follows validated ledgers on your own node's admin JSON-RPC through
`docker exec <container> curl` (the admin port is never exposed), fetching each
ledger once with `transactions: true, expand: true`, at most --rate requests/s.

Outputs (in --out):
  tx.csv        one row per transaction (account HMAC-truncated, see below)
  ledgers.csv   one row per fetched ledger (hash, close time, tx count)
  samples.jsonl every --sample-interval s: validated seq, base fee, state
  gaps.csv      ledgers that could not be fetched after retries
  state.json    window start/end and start seq, so a restart resumes the window
  collect.log   (--log) start/resume, hourly progress, gaps, final summary

Accounts are never written in clear: `account` is the first 16 hex chars of
HMAC-SHA256(key, address), with a random key created in --key-file (mode 0600,
never published). The same address maps to the same token within one run, so
activity concentration can be measured without publishing addresses.

Only read-only RPC methods are sent: server_info, ledger.
"""

import argparse
import csv
import hashlib
import hmac
import json
import os
import platform
import secrets
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

TX_FIELDS = [
    "ledger_index", "close_time", "close_time_iso", "tx_index", "tx_hash", "tx_type", "result",
    "fee_drops", "account", "memo", "signers", "delivered_class", "accounts_created",
]
LEDGER_FIELDS = ["ledger_index", "ledger_hash", "close_time", "close_time_iso", "txn_count", "fetched_utc"]
GAP_FIELDS = ["ledger_index", "error", "logged_at"]
READ_ONLY_METHODS = {"server_info", "ledger"}
RIPPLE_EPOCH = 946684800  # 2000-01-01T00:00:00Z

_stop = False


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ripple_iso(t):
    return datetime.fromtimestamp(int(t) + RIPPLE_EPOCH, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(fh, msg):
    line = f"{utcnow()} {msg}"
    print(line, flush=True)
    if fh:
        fh.write(line + "\n")
        fh.flush()


class Rpc:
    """docker exec JSON-RPC client with a simple request-rate limiter."""

    def __init__(self, container, port, rate, docker_cmd):
        self.container = container
        self.url = f"http://127.0.0.1:{port}/"
        self.min_gap = 1.0 / rate
        self.docker_cmd = docker_cmd
        self.last = 0.0
        self.count = 0

    def call(self, method, params=None):
        if method not in READ_ONLY_METHODS:
            raise ValueError(f"refusing non read-only method {method}")
        wait = self.min_gap - (time.monotonic() - self.last)
        if wait > 0:
            time.sleep(wait)
        self.last = time.monotonic()
        self.count += 1
        body = json.dumps({"method": method, "params": [params or {}]})
        cmd = self.docker_cmd + [
            "exec", self.container, "curl", "-s", "-m", "15", "-X", "POST",
            "-H", "Content-Type: application/json", "-d", body, self.url,
        ]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
        if out.returncode != 0:
            raise RuntimeError(f"docker exec rc={out.returncode}: {out.stderr.strip()[:200]}")
        result = json.loads(out.stdout)["result"]
        if result.get("status") == "error":
            raise LookupError(result.get("error", "unknown"))
        return result


def load_key(path):
    """Return the HMAC key, creating a random 32-byte key (mode 0600) if absent."""
    if not os.path.exists(path):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(secrets.token_hex(32))
    with open(path) as fh:
        return bytes.fromhex(fh.read().strip())


def hash_account(key, address):
    return hmac.new(key, address.encode(), hashlib.sha256).hexdigest()[:16]


def delivered_class(meta):
    """none (not a delivering tx), native (drops string), issued (IOU/MPT object), unavailable."""
    if "delivered_amount" not in meta:
        return "none"
    d = meta["delivered_amount"]
    if d == "unavailable":
        return "unavailable"
    return "native" if isinstance(d, str) else "issued"


def accounts_created(meta):
    n = 0
    for node in meta.get("AffectedNodes", []):
        c = node.get("CreatedNode")
        if c and c.get("LedgerEntryType") == "AccountRoot":
            n += 1
    return n


def tx_rows(ledger, key):
    idx = int(ledger["ledger_index"])
    ct = int(ledger["close_time"])
    rows = []
    for tx in ledger.get("transactions") or []:
        meta = tx.get("metaData") or tx.get("meta") or {}
        rows.append({
            "ledger_index": idx,
            "close_time": ct,
            "close_time_iso": ripple_iso(ct),
            "tx_index": meta.get("TransactionIndex", ""),
            "tx_hash": tx.get("hash", ""),
            "tx_type": tx.get("TransactionType", ""),
            "result": meta.get("TransactionResult", ""),
            "fee_drops": int(tx.get("Fee", 0)),
            "account": hash_account(key, tx["Account"]) if tx.get("Account") else "",
            "memo": "y" if tx.get("Memos") else "n",
            "signers": len(tx.get("Signers") or []),
            "delivered_class": delivered_class(meta),
            "accounts_created": accounts_created(meta),
        })
    rows.sort(key=lambda r: (r["tx_index"] if r["tx_index"] != "" else 0))
    return rows


def ledger_row(ledger, n):
    ct = int(ledger["close_time"])
    return {
        "ledger_index": int(ledger["ledger_index"]), "ledger_hash": ledger["ledger_hash"],
        "close_time": ct, "close_time_iso": ripple_iso(ct), "txn_count": n, "fetched_utc": utcnow(),
    }


def append_rows(path, fields, rows):
    new = not os.path.exists(path)
    with open(path, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        if new:
            w.writeheader()
        w.writerows(rows)


def last_index(path):
    """Last ledger_index in ledgers.csv/gaps.csv, or None."""
    if not os.path.exists(path):
        return None
    last = None
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            last = int(row["ledger_index"])
    return last


def drop_torn_tail(path):
    """Cut a partial last line (a kill during an append) so resumed rows start on a fresh line.

    Returns the number of bytes removed (0 if the file is missing, empty or ends with a newline).
    """
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return 0
    with open(path, "rb+") as fh:
        data = fh.read()
        if data.endswith(b"\n"):
            return 0
        keep = data.rfind(b"\n") + 1
        fh.truncate(keep)
        return len(data) - keep


def drop_orphan_tx(path, done):
    """Cut trailing tx.csv rows whose ledger_index > done (written before a kill, ledger row missing).

    The collector appends a ledger's tx rows before its ledgers.csv row, so after a kill between
    the two appends those rows would be written again on resume. Returns the number of rows removed.
    """
    if not os.path.exists(path):
        return 0
    with open(path, "rb+") as fh:
        lines = fh.read().split(b"\n")
        if lines and lines[-1] == b"":
            lines.pop()
        keep = len(lines)
        while keep > 1 and int(lines[keep - 1].split(b",", 1)[0]) > done:
            keep -= 1
        if keep == len(lines):
            return 0
        fh.truncate(sum(len(x) + 1 for x in lines[:keep]))
        return len(lines) - keep


def count_rows(path):
    if not os.path.exists(path):
        return 0
    with open(path) as fh:
        return max(0, sum(1 for _ in fh) - 1)


def _handle(signum, _frame):
    global _stop
    _stop = True


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="data/txmix")
    p.add_argument("--log", default=None, help="append log lines to this file too")
    p.add_argument("--key-file", default=None, help="HMAC key file (default <out>/.hmac_key, 0600)")
    p.add_argument("--container", default="postfiatd")
    p.add_argument("--port", type=int, default=5005)
    p.add_argument("--rate", type=float, default=1.0, help="max requests/second (default 1, max 1)")
    p.add_argument("--hours", type=float, default=24.0, help="window length from first start")
    p.add_argument("--start", type=int, default=None, help="first ledger (default: current validated)")
    p.add_argument("--sample-interval", type=int, default=600)
    p.add_argument("--retries", type=int, default=3)
    p.add_argument("--no-sudo", action="store_true", help="call docker without sudo")
    a = p.parse_args(argv)

    if a.rate > 1.0:
        p.error("--rate above 1 req/s is not allowed")
    os.makedirs(a.out, exist_ok=True)
    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)
    lf = open(a.log, "a") if a.log else None
    key = load_key(a.key_file or os.path.join(a.out, ".hmac_key"))
    rpc = Rpc(a.container, a.port, a.rate, ["docker"] if a.no_sudo else ["sudo", "docker"])

    tx_csv = os.path.join(a.out, "tx.csv")
    ledgers_csv = os.path.join(a.out, "ledgers.csv")
    gaps_csv = os.path.join(a.out, "gaps.csv")
    samples = os.path.join(a.out, "samples.jsonl")
    state_path = os.path.join(a.out, "state.json")

    os_line = "unknown"
    if os.path.exists("/etc/os-release"):
        with open("/etc/os-release") as fh:
            kv = dict(line.rstrip().split("=", 1) for line in fh if "=" in line)
        os_line = kv.get("PRETTY_NAME", "unknown").strip('"')
    log(lf, f"OS: {os_line} | kernel {platform.release()} | python {platform.python_version()}")

    info = rpc.call("server_info")["info"]
    log(lf, f"node: build {info.get('build_version')} state {info.get('server_state')} "
            f"complete_ledgers {info.get('complete_ledgers')}")
    validated = int(info["validated_ledger"]["seq"])

    state = {}
    if os.path.exists(state_path):
        with open(state_path) as fh:
            state = json.load(fh)
    if state:
        for path in (tx_csv, ledgers_csv, gaps_csv):
            torn = drop_torn_tail(path)
            if torn:
                log(lf, f"RESUME dropped torn last line of {os.path.basename(path)} ({torn} bytes)")
    done = max(x for x in (last_index(ledgers_csv), last_index(gaps_csv), -1) if x is not None)
    if state:
        # a restart before the first ledger row resumes at the window's start seq, same window
        done = max(done, state["start_seq"] - 1)
        orphans = drop_orphan_tx(tx_csv, done)
        if orphans:
            log(lf, f"RESUME dropped {orphans} tx.csv rows after ledger {done} (no ledgers.csv row yet)")
        nxt = done + 1
        log(lf, f"RESUME at ledger {nxt} (window {state['start_utc']} -> {state['end_utc']}, "
                f"start seq {state['start_seq']}, behind by {validated - nxt + 1})")
    else:
        nxt = a.start if a.start is not None else validated
        now = time.time()
        state = {"start": int(now), "end": int(now + a.hours * 3600), "start_seq": nxt,
                 "start_utc": utcnow(),
                 "end_utc": datetime.fromtimestamp(now + a.hours * 3600, timezone.utc)
                 .strftime("%Y-%m-%dT%H:%M:%SZ")}
        with open(state_path, "w") as fh:
            json.dump(state, fh, indent=1)
        log(lf, f"WINDOW START {state['start_utc']} -> {state['end_utc']} at ledger {nxt}, "
                f"rate <= {a.rate} req/s")

    next_sample = 0.0
    next_progress = time.monotonic() + 3600
    while not _stop and time.time() < state["end"]:
        now = time.monotonic()
        if now >= next_sample:
            try:
                i = rpc.call("server_info")["info"]
                vl = i.get("validated_ledger", {})
                validated = int(vl.get("seq", validated))
                with open(samples, "a") as fh:
                    fh.write(json.dumps({"sampled_at": utcnow(), "validated_seq": vl.get("seq"),
                                         "base_fee_xrp": vl.get("base_fee_xrp"),
                                         "reserve_base_xrp": vl.get("reserve_base_xrp"),
                                         "load_factor": i.get("load_factor"),
                                         "server_state": i.get("server_state")}) + "\n")
            except Exception as e:
                log(lf, f"WARN sample failed: {e}")
            next_sample = now + a.sample_interval
            continue
        if now >= next_progress:
            log(lf, f"PROGRESS last={nxt - 1} lag={validated - nxt + 1} ledgers={count_rows(ledgers_csv)} "
                    f"tx={count_rows(tx_csv)} gaps={count_rows(gaps_csv)} requests={rpc.count}")
            next_progress = now + 3600
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
                led = rpc.call("ledger", {"ledger_index": nxt, "transactions": True,
                                          "expand": True})["ledger"]
                rows = tx_rows(led, key)
                append_rows(tx_csv, TX_FIELDS, rows)
                append_rows(ledgers_csv, LEDGER_FIELDS, [ledger_row(led, len(rows))])
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

    reason = "signal" if _stop else "window end reached"
    log(lf, f"STOP ({reason}) requests this run={rpc.count}")
    log(lf, f"SUMMARY window {state['start_utc']} -> {state['end_utc']} "
            f"ledgers {state['start_seq']}-{nxt - 1} fetched={count_rows(ledgers_csv)} "
            f"tx_rows={count_rows(tx_csv)} gaps={count_rows(gaps_csv)}")
    if lf:
        lf.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
