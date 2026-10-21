#!/usr/bin/env python3
"""Resumable, read-only Post Fiat ledger collector.

Talks to your own node's admin JSON-RPC through `docker exec <container> curl`
(so the admin port never has to be exposed), at most --rate requests/second.

Outputs (in --out):
  ledgers.csv     one row per validated ledger, consecutive from the start seq
  samples.jsonl   every --sample-interval s: validator count, quorum, proposers
  skips.csv       every ledger that could not be fetched after retries
  state.json      start time / start seq, used to resume after interruption

Only uses read-only RPC methods: server_info, ledger, validators.
"""

import argparse
import csv
import json
import os
import platform
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

LEDGER_FIELDS = [
    "ledger_index", "ledger_hash", "close_time", "close_time_iso",
    "close_time_resolution", "parent_close_time", "close_flags",
    "txn_count", "total_coins",
]
SKIP_FIELDS = ["ledger_index", "error", "logged_at"]
READ_ONLY_METHODS = {"server_info", "ledger", "validators"}
RIPPLE_EPOCH = 946684800  # 2000-01-01T00:00:00Z

_stop = False


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
            "exec", self.container, "curl", "-s", "-m", "10", "-X", "POST",
            "-H", "Content-Type: application/json", "-d", body, self.url,
        ]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            raise RuntimeError(f"docker exec rc={out.returncode}: {out.stderr.strip()[:200]}")
        result = json.loads(out.stdout)["result"]
        if result.get("status") == "error":
            raise LookupError(result.get("error", "unknown"))
        return result


def ledger_row(ledger):
    txs = ledger.get("transactions") or []
    close_time = int(ledger["close_time"])
    return {
        "ledger_index": int(ledger["ledger_index"]),
        "ledger_hash": ledger["ledger_hash"],
        "close_time": close_time,
        "close_time_iso": datetime.fromtimestamp(close_time + RIPPLE_EPOCH, timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "close_time_resolution": int(ledger["close_time_resolution"]),
        "parent_close_time": int(ledger["parent_close_time"]),
        "close_flags": int(ledger.get("close_flags", 0)),
        "txn_count": len(txs),
        "total_coins": int(ledger["total_coins"]),
    }


def last_csv_index(path):
    """Return the last ledger_index in an existing ledgers.csv, or None."""
    if not os.path.exists(path):
        return None
    last = None
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            last = int(row["ledger_index"])
    return last


def append_csv(path, fields, row):
    new = not os.path.exists(path)
    with open(path, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        if new:
            w.writeheader()
        w.writerow(row)


def take_sample(rpc):
    info = rpc.call("server_info")["info"]
    vals = rpc.call("validators")
    lc = info.get("last_close", {})
    vl = info.get("validated_ledger", {})
    return {
        "sampled_at": utcnow(),
        "validated_seq": vl.get("seq"),
        "server_state": info.get("server_state"),
        "proposers": lc.get("proposers"),
        "converge_time_s": lc.get("converge_time_s"),
        "validation_quorum": vals.get("validation_quorum", info.get("validation_quorum")),
        "trusted_validators": len(vals.get("trusted_validator_keys", [])),
        "peers": info.get("peers"),
    }


def _handle(signum, _frame):
    global _stop
    _stop = True


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="data")
    p.add_argument("--log", default=None, help="append log lines to this file too")
    p.add_argument("--container", default="postfiatd")
    p.add_argument("--port", type=int, default=5005)
    p.add_argument("--rate", type=float, default=1.0, help="max requests/second (default 1)")
    p.add_argument("--duration", type=int, default=86400, help="seconds from first start (default 24h)")
    p.add_argument("--target-ledgers", type=int, default=20000,
                   help="informational: report whether this many consecutive ledgers were collected")
    p.add_argument("--sample-interval", type=int, default=300)
    p.add_argument("--start", type=int, default=None, help="first ledger (default: current validated)")
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

    ledgers_csv = os.path.join(a.out, "ledgers.csv")
    samples_jsonl = os.path.join(a.out, "samples.jsonl")
    skips_csv = os.path.join(a.out, "skips.csv")
    state_path = os.path.join(a.out, "state.json")

    os_line = "unknown"
    if os.path.exists("/etc/os-release"):
        with open("/etc/os-release") as fh:
            kv = dict(line.rstrip().split("=", 1) for line in fh if "=" in line)
        os_line = kv.get("PRETTY_NAME", "unknown").strip('"')
    log(lf, f"OS: {os_line} | kernel {platform.release()} | python {platform.python_version()}")

    state = {}
    if os.path.exists(state_path):
        with open(state_path) as fh:
            state = json.load(fh)
    info = rpc.call("server_info")["info"]
    log(lf, f"node: build {info.get('build_version')} state {info.get('server_state')} "
            f"complete_ledgers {info.get('complete_ledgers')}")
    validated = int(info["validated_ledger"]["seq"])

    last = last_csv_index(ledgers_csv)
    if last is not None:
        nxt = last + 1
        log(lf, f"RESUME from ledger {nxt} (start {state.get('start_seq')} at {state.get('started_at')})")
    else:
        nxt = a.start if a.start is not None else validated
        state = {"started_at": utcnow(), "start_epoch": time.time(), "start_seq": nxt}
        log(lf, f"START at ledger {nxt}, duration {a.duration}s, rate <= {a.rate} req/s")
    with open(state_path, "w") as fh:
        json.dump(state, fh, indent=1)

    deadline = state["start_epoch"] + a.duration
    next_sample = 0.0
    fetched = 0
    while not _stop and time.time() < deadline:
        now = time.monotonic()
        if now >= next_sample:
            try:
                s = take_sample(rpc)
                with open(samples_jsonl, "a") as fh:
                    fh.write(json.dumps(s) + "\n")
                validated = int(s["validated_seq"] or validated)
                log(lf, f"SAMPLE seq={s['validated_seq']} proposers={s['proposers']} "
                        f"quorum={s['validation_quorum']} trusted={s['trusted_validators']} "
                        f"state={s['server_state']}")
            except Exception as e:  # keep collecting ledgers if a sample fails
                log(lf, f"WARN sample failed: {e}")
            next_sample = now + a.sample_interval
            continue
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
                                          "expand": False})["ledger"]
                append_csv(ledgers_csv, LEDGER_FIELDS, ledger_row(led))
                err = None
                break
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                log(lf, f"WARN ledger {nxt} attempt {attempt}/{a.retries}: {err}")
        if err:
            append_csv(skips_csv, SKIP_FIELDS, {"ledger_index": nxt, "error": err, "logged_at": utcnow()})
            log(lf, f"SKIP ledger {nxt}: {err}")
        fetched += 1
        if fetched % 500 == 0:
            log(lf, f"PROGRESS last={nxt} lag={validated - nxt} requests={rpc.count}")
        nxt += 1

    first = state["start_seq"]
    last = last_csv_index(ledgers_csv)
    skips = []
    if os.path.exists(skips_csv):
        with open(skips_csv, newline="") as fh:
            skips = [int(r["ledger_index"]) for r in csv.DictReader(fh)]
    n = (last - first + 1) if last is not None else 0
    reason = "signal" if _stop else "duration reached"
    log(lf, f"STOP ({reason}) after {rpc.count} requests this run")
    log(lf, f"RANGE {first}-{last} ledgers={n} consecutive={'yes' if not skips else 'no'} "
            f"skipped={len(skips)} skip_list={skips if skips else 'none'} "
            f"target_{a.target_ledgers}={'met' if n >= a.target_ledgers else 'not met'}")
    if lf:
        lf.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
