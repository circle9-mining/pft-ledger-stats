#!/usr/bin/env python3
"""Resumable, read-only per-validator validations collector (Python 3 stdlib only).

Subscribes to your own node's `validations` and `ledger` streams on the admin
WebSocket, which stays bound to loopback: the WebSocket client ("relay", the
`relay` subcommand of this same file) runs inside the node container via
`docker exec -i <container> timeout <s> python3 -I -c <this file> relay ...`,
and the host side only exchanges JSON lines with it over the exec pipe.

Only these commands are ever sent (enforced on both sides): subscribe,
server_info, validators, manifest. Nothing on the node is changed.

Outputs (in --out):
  validations.csv   one row per validation received
  ledgers.csv       one row per validated ledger (ledgerClosed stream message)
  manifests.csv     one row per `manifest` lookup (key -> master key, domain)
  snapshots.jsonl   `server_info` / `validators` results at start and hourly
  gaps.csv          ledgers missing from the ledger stream (disconnects, restarts)
  collect.log       window start/end, connects, hourly row counts, gaps, summary
  window.json       fixed window start/end, so a restart resumes the same window

Usage:
  ./collect_validations.py --out data/validators --hours 24
"""

import argparse
import csv
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

ALLOWED_COMMANDS = {"subscribe", "server_info", "validators", "manifest"}
STREAMS = ["validations", "ledger"]
RIPPLE_EPOCH = 946684800  # 2000-01-01T00:00:00Z

VALIDATION_FIELDS = [
    "ledger_index", "ledger_hash", "master_key", "master_source", "signing_key",
    "signing_time", "signing_time_iso", "recv_utc", "full",
]
LEDGER_FIELDS = [
    "ledger_index", "ledger_hash", "ledger_time", "ledger_time_iso", "txn_count", "recv_utc",
]
MANIFEST_FIELDS = ["queried_key", "master_key", "ephemeral_key", "seq", "domain", "recv_utc"]
PUBLISHER_FIELDS = ("pubkey_publisher", "seq", "effective", "expiration")
GAP_FIELDS = ["first_missing", "last_missing", "count", "detected_utc", "reason"]


# ---------------------------------------------------------------- shared helpers

def utcnow_ms():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def ripple_to_iso(t):
    return datetime.fromtimestamp(int(t) + RIPPLE_EPOCH, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def check_command(obj):
    """Raise ValueError unless obj is a request for one allowed command."""
    cmd = obj.get("command") if isinstance(obj, dict) else None
    if cmd not in ALLOWED_COMMANDS:
        raise ValueError(f"command not allowed: {cmd!r}")
    if cmd == "subscribe" and set(obj) - {"id", "command", "streams"}:
        raise ValueError("subscribe: only `streams` is allowed")
    if cmd == "subscribe" and not set(obj.get("streams", [])) <= set(STREAMS):
        raise ValueError(f"subscribe: streams not allowed: {obj.get('streams')}")
    return obj


# ---------------------------------------------------------------- relay (runs in the container)

def relay_main(host, port):
    """Minimal WebSocket client: stdin JSON commands -> node, node messages -> stdout."""
    import base64
    import socket
    import struct
    import threading

    s = socket.create_connection((host, port), timeout=120)
    key = base64.b64encode(os.urandom(16)).decode()
    s.sendall((f"GET / HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
               f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n").encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = s.recv(4096)
        if not chunk:
            raise ConnectionError("closed during handshake")
        buf += chunk
    head, buf = buf.split(b"\r\n\r\n", 1)
    if b" 101 " not in head.split(b"\r\n")[0]:
        raise ConnectionError(head.split(b"\r\n")[0].decode(errors="replace"))
    lock = threading.Lock()

    def send(opcode, data):
        mask = os.urandom(4)
        n = len(data)
        if n < 126:
            hdr = struct.pack(">BB", 0x80 | opcode, 0x80 | n)
        elif n < 65536:
            hdr = struct.pack(">BBH", 0x80 | opcode, 0x80 | 126, n)
        else:
            hdr = struct.pack(">BBQ", 0x80 | opcode, 0x80 | 127, n)
        with lock:
            s.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def stdin_loop():
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                send(0x1, json.dumps(check_command(json.loads(line))).encode())
            except ValueError as e:
                sys.stderr.write(f"relay refused: {e}\n")
        os._exit(0)  # host closed the pipe

    threading.Thread(target=stdin_loop, daemon=True).start()

    def read(n):
        nonlocal buf
        while len(buf) < n:
            chunk = s.recv(65536)
            if not chunk:
                raise ConnectionError("connection closed")
            buf += chunk
        out, buf = buf[:n], buf[n:]
        return out

    parts = []
    while True:
        b0, b1 = read(2)
        n = b1 & 0x7F
        if n == 126:
            n = struct.unpack(">H", read(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", read(8))[0]
        mask = read(4) if b1 & 0x80 else None
        payload = read(n)
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        opcode = b0 & 0x0F
        if opcode == 0x9:
            send(0xA, payload)
        elif opcode == 0x8:
            raise ConnectionError("server sent close")
        elif opcode in (0x0, 0x1):
            parts.append(payload)
            if b0 & 0x80:
                msg = json.loads(b"".join(parts))
                parts = []
                msg.pop("data", None)  # serialized STValidation blob, not needed
                msg.pop("signature", None)
                msg["recv_utc"] = utcnow_ms()
                sys.stdout.write(json.dumps(msg, separators=(",", ":")) + "\n")
                sys.stdout.flush()


# ---------------------------------------------------------------- host side

def snapshot_fields(command, res):
    """Keep only public network fields (no host ids, node keys, ports or peer addresses)."""
    if command == "server_info":
        i = res.get("info", {})
        return {"build_version": i.get("build_version"), "server_state": i.get("server_state"),
                "validation_quorum": i.get("validation_quorum"),
                "validated_ledger_seq": (i.get("validated_ledger") or {}).get("seq")}
    if command == "validators":
        return {"validation_quorum": res.get("validation_quorum"),
                "trusted_validator_keys": res.get("trusted_validator_keys", []),
                "publisher_lists": [{k: pl.get(k) for k in PUBLISHER_FIELDS}
                                    for pl in res.get("publisher_lists", [])]}
    return {}


def drop_torn_tail(path):
    """Cut a partial last line (a kill during a buffer flush) so resumed rows start on a fresh line.

    Returns the number of bytes removed (0 if the file is missing, empty or ends with a newline).
    """
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return 0
    with open(path, "rb+") as f:
        data = f.read()
        if data.endswith(b"\n"):
            return 0
        keep = data.rfind(b"\n") + 1
        f.truncate(keep)
        return len(data) - keep


class Csv:
    """Append-only CSV with a header written once."""

    def __init__(self, path, fields):
        self.torn = drop_torn_tail(path)
        new = not os.path.exists(path) or os.path.getsize(path) == 0
        self.f = open(path, "a", newline="")
        self.w = csv.DictWriter(self.f, fieldnames=fields, extrasaction="ignore")
        self.rows = 0
        if new:
            self.w.writeheader()

    def write(self, row):
        self.w.writerow(row)
        self.rows += 1

    def flush(self):
        self.f.flush()


def last_ledger_index(path):
    """Last ledger_index in ledgers.csv, or None."""
    if not os.path.exists(path):
        return None
    last = None
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            last = int(row["ledger_index"])
    return last


def count_rows(path):
    if not os.path.exists(path):
        return 0
    with open(path) as f:
        return max(0, sum(1 for _ in f) - 1)


class Collector:
    """Turns relay messages into CSV rows. Independent of the transport, so it is unit-testable."""

    def __init__(self, out, send, log):
        self.out, self.send, self.log = out, send, log
        self.val = Csv(os.path.join(out, "validations.csv"), VALIDATION_FIELDS)
        self.led = Csv(os.path.join(out, "ledgers.csv"), LEDGER_FIELDS)
        self.man = Csv(os.path.join(out, "manifests.csv"), MANIFEST_FIELDS)
        self.gaps = Csv(os.path.join(out, "gaps.csv"), GAP_FIELDS)
        for c in (self.val, self.led, self.man, self.gaps):
            if c.torn:
                self.log(f"RESUME dropped torn last line of {os.path.basename(c.f.name)} ({c.torn} bytes)")
        self.snap = open(os.path.join(out, "snapshots.jsonl"), "a")
        self.last_ledger = last_ledger_index(os.path.join(out, "ledgers.csv"))
        self.master_of = {}    # signing key -> master key (from stream or manifest)
        self.looked_up = set()  # keys a manifest request was sent for
        self.pending = {}      # signing key -> rows waiting for its manifest
        self.reconnected = False
        self.next_id = 0
        self.req_kind = {}
        if os.path.exists(os.path.join(out, "manifests.csv")):
            with open(os.path.join(out, "manifests.csv"), newline="") as f:
                for r in csv.DictReader(f):
                    self.looked_up.add(r["queried_key"])
                    if r["master_key"]:
                        self.master_of[r["queried_key"]] = r["master_key"]
                        if r["ephemeral_key"]:
                            self.master_of[r["ephemeral_key"]] = r["master_key"]

    def request(self, obj, kind):
        self.next_id += 1
        obj = dict(obj, id=self.next_id)
        self.req_kind[self.next_id] = kind
        self.send(check_command(obj))

    def lookup(self, key):
        if key not in self.looked_up:
            self.looked_up.add(key)
            self.request({"command": "manifest", "public_key": key}, ("manifest", key))

    def on_connect(self):
        self.reconnected = True
        self.request({"command": "server_info"}, ("snapshot", "server_info"))
        self.request({"command": "validators"}, ("snapshot", "validators"))
        self.request({"command": "subscribe", "streams": STREAMS}, ("subscribe", None))

    def snapshots(self):
        self.request({"command": "server_info"}, ("snapshot", "server_info"))
        self.request({"command": "validators"}, ("snapshot", "validators"))

    def handle(self, msg):
        t = msg.get("type")
        if t == "validationReceived":
            self.on_validation(msg)
        elif t == "ledgerClosed":
            self.on_ledger(msg)
        elif t == "response" or "result" in msg:
            self.on_response(msg)

    def on_validation(self, m):
        sk = m.get("validation_public_key", "")
        row = {
            "ledger_index": int(m["ledger_index"]),
            "ledger_hash": m.get("ledger_hash", ""),
            "signing_key": sk,
            "signing_time": m.get("signing_time", ""),
            "signing_time_iso": ripple_to_iso(m["signing_time"]) if "signing_time" in m else "",
            "recv_utc": m.get("recv_utc", ""),
            "full": int(bool(m.get("full", False))),
        }
        mk = m.get("master_key")
        if mk:
            self.master_of[sk] = mk
            self.lookup(mk)  # once per master key: domain for the report
            self.val.write(dict(row, master_key=mk, master_source="stream"))
        elif sk in self.master_of:
            self.val.write(dict(row, master_key=self.master_of[sk], master_source="manifest"))
        else:
            self.pending.setdefault(sk, []).append(row)
            self.lookup(sk)

    def on_ledger(self, m):
        idx = int(m["ledger_index"])
        if self.last_ledger is not None and idx > self.last_ledger + 1:
            reason = "reconnect/restart" if self.reconnected else "skipped in stream"
            self.gaps.write({"first_missing": self.last_ledger + 1, "last_missing": idx - 1,
                             "count": idx - 1 - self.last_ledger, "detected_utc": m.get("recv_utc", ""),
                             "reason": reason})
            self.log(f"GAP ledgers {self.last_ledger + 1}-{idx - 1} ({idx - 1 - self.last_ledger}) {reason}")
            self.gaps.flush()
        if self.last_ledger is not None and idx <= self.last_ledger:
            return  # already recorded (stream replays the current ledger on subscribe)
        self.reconnected = False  # only after the first NEW ledger, so a replay can't relabel the gap
        self.last_ledger = idx
        self.led.write({"ledger_index": idx, "ledger_hash": m.get("ledger_hash", ""),
                        "ledger_time": m.get("ledger_time", ""),
                        "ledger_time_iso": ripple_to_iso(m["ledger_time"]) if "ledger_time" in m else "",
                        "txn_count": m.get("txn_count", ""), "recv_utc": m.get("recv_utc", "")})
        self.flush()  # once per ledger (~3-4 s): a kill loses at most the current ledger's rows

    def on_response(self, m):
        kind = self.req_kind.pop(m.get("id"), None)
        if kind is None:
            return
        res = m.get("result", {})
        if kind[0] == "snapshot":
            self.snap.write(json.dumps({"recv_utc": m.get("recv_utc", ""), "command": kind[1],
                                        "status": m.get("status"), "result": snapshot_fields(kind[1], res)},
                                       separators=(",", ":")) + "\n")
            self.snap.flush()
        elif kind[0] == "subscribe":
            self.log(f"subscribe status={m.get('status')}")
        elif kind[0] == "manifest":
            key = kind[1]
            d = res.get("details") or {}
            mk = d.get("master_key", "")
            self.man.write({"queried_key": key, "master_key": mk, "ephemeral_key": d.get("ephemeral_key", ""),
                            "seq": d.get("seq", ""), "domain": d.get("domain", ""),
                            "recv_utc": m.get("recv_utc", "")})
            self.man.flush()
            if mk:
                self.master_of[key] = mk
            for row in self.pending.pop(key, []):
                self.val.write(dict(row, master_key=mk or key,
                                    master_source="manifest" if mk else "unresolved"))

    def flush(self, final=False):
        if final:  # never drop rows whose manifest answer did not arrive
            for key, rows in self.pending.items():
                for row in rows:
                    self.val.write(dict(row, master_key=key, master_source="unresolved"))
            self.pending = {}
        for c in (self.val, self.led, self.man, self.gaps):
            c.flush()


def relay_cmd(docker, container, left, port):
    with open(os.path.abspath(__file__)) as f:
        src = f.read()
    return docker + ["exec", "-i", container, "timeout", str(left), "python3", "-I", "-c", src,
                     "relay", "127.0.0.1", str(port)]


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", required=True)
    p.add_argument("--hours", type=float, default=24.0, help="window length (default 24)")
    p.add_argument("--container", default="postfiatd")
    p.add_argument("--port", type=int, default=6006, help="admin WebSocket port inside the container")
    p.add_argument("--no-sudo", action="store_true", help="call docker without sudo")
    a = p.parse_args()

    os.makedirs(a.out, exist_ok=True)
    logf = open(os.path.join(a.out, "collect.log"), "a")

    def log(s):
        logf.write(f"{utcnow_ms()} {s}\n")
        logf.flush()

    wpath = os.path.join(a.out, "window.json")
    if os.path.exists(wpath):
        with open(wpath) as f:
            win = json.load(f)
        log(f"RESUME window {win['start_utc']} -> {win['end_utc']}")
    else:
        now = int(time.time())
        win = {"start": now, "end": now + int(a.hours * 3600),
               "start_utc": datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        win["end_utc"] = datetime.fromtimestamp(win["end"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(wpath, "w") as f:
            json.dump(win, f)
        log(f"WINDOW start={win['start_utc']} end={win['end_utc']} container={a.container} port={a.port} "
            f"commands={','.join(sorted(ALLOWED_COMMANDS))}")

    def on_term(signum, _frame):
        raise SystemExit(f"signal {signum}")

    signal.signal(signal.SIGTERM, on_term)
    docker = ["docker"] if a.no_sudo else ["sudo", "docker"]
    relay = {}  # current relay process

    def send(obj):
        relay["proc"].stdin.write(json.dumps(obj) + "\n")
        relay["proc"].stdin.flush()

    col = Collector(a.out, send, log)
    try:
        collect(a, win, docker, col, log, relay)
    except SystemExit as e:
        col.flush()
        log(f"stopped ({e}); rerun the same command to resume the window")
        raise
    col.flush(final=True)
    out = {n: count_rows(os.path.join(a.out, n)) for n in
           ("validations.csv", "ledgers.csv", "manifests.csv", "gaps.csv")}
    log(f"END window {win['start_utc']} -> {win['end_utc']} rows: " +
        " ".join(f"{k}={v}" for k, v in out.items()))


def collect(a, win, docker, col, log, relay):
    """Run relays until the window ends, reconnecting after each disconnect."""
    next_progress = time.time() + 3600
    while True:
        left = win["end"] - int(time.time())
        if left <= 0:
            break
        log(f"connect (remaining {left}s, last ledger {col.last_ledger})")
        proc = subprocess.Popen(relay_cmd(docker, a.container, left, a.port), stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
        relay["proc"] = proc
        col.on_connect()
        for line in proc.stdout:
            try:
                col.handle(json.loads(line))
            except (ValueError, KeyError) as e:
                log(f"bad message skipped: {e}: {line[:120]!r}")
            now = time.time()
            if now >= next_progress:
                next_progress = now + 3600
                col.flush()
                col.snapshots()
                log(f"progress validations={count_rows(os.path.join(a.out, 'validations.csv'))} "
                    f"ledgers={count_rows(os.path.join(a.out, 'ledgers.csv'))} "
                    f"gaps={count_rows(os.path.join(a.out, 'gaps.csv'))} last_ledger={col.last_ledger}")
        proc.stdin.close()
        rc = proc.wait()
        err = proc.stderr.read().strip().replace("\n", " | ")
        col.flush()
        log(f"relay exited rc={rc}{' stderr: ' + err[:300] if err else ''}")
        if win["end"] - int(time.time()) > 0:
            time.sleep(5)


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "relay":
        try:
            relay_main(sys.argv[2], int(sys.argv[3]))
        except (ConnectionError, OSError) as e:
            sys.stderr.write(f"{utcnow_ms()} disconnect: {e}\n")
            sys.exit(1)
    else:
        main()
