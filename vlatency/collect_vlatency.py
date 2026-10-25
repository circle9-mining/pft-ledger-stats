#!/usr/bin/env python3
"""Resumable, read-only validation-latency collector (Python 3 stdlib only).

Subscribes to the `validations` and `ledger` streams of your own node's loopback admin
WebSocket. Nothing else is ever sent (no submit, no sign, no other command). Run it inside the
node container's network namespace, because the admin port listens on the container's own
loopback (see run-collector.sh).

Output: --out/vl.jsonl, append-only, one JSON object per line, `ev` is one of
  session  collector (re)start: {start, resumed, last_ledger, last_t, torn_bytes, bad_lines, window_end}
  ledger   validated ledger closed: {li, lh, ct, txn, t}
           ct = ledger close_time in Ripple-epoch seconds, exactly as the node sent it
  val      first time this node saw a validation (key, ledger index, ledger hash):
           {k, mk, li, lh, st, vh, full, t}
           k = validation_public_key, mk = master_key (if any), st = signing_time (Ripple epoch),
           vh = validated_hash (the signer's last fully validated ledger, if sent), full = full/partial
  gap      ledgers the ledger stream never delivered: {from, to, count, reason, t}
           reason = stream_skip (same connection) | reconnect | restart (collector was down)
  stop     clean stop: {t, reason}
`t` is this host's UTC clock in unix seconds when the message was read (single vantage point).

Window: state.json keeps {window_start, window_end}; a restart with the same --out resumes the
same window. A torn last line (kill during a write) is cut on start and counted in the session
event. Duplicates (the same key/index/hash relayed again, or replayed around a resume) are dropped
by the collector for the last KEEP_LEDGERS ledgers; the analyzer dedupes again over the whole file.
"""

import argparse
import json
import os
import signal
import socket
import sys
import time
from datetime import datetime, timezone

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(here, "..", "txflow"))
from wsclient import WsClient  # noqa: E402

STREAMS = ["validations", "ledger"]
ALLOWED_COMMANDS = {"subscribe"}
KEEP_LEDGERS = 2000
_stop = None


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def drop_torn_tail(path):
    """Cut a partial last line. Returns the number of bytes removed."""
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return 0
    with open(path, "rb+") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        # scan back in blocks for the last newline (files are large; never read them whole)
        pos, keep = size, 0
        while pos > 0:
            step = min(65536, pos)
            pos -= step
            fh.seek(pos)
            block = fh.read(step)
            if pos + step == size and block.endswith(b"\n"):
                return 0
            i = block.rfind(b"\n")
            if i >= 0:
                keep = pos + i + 1
                break
        fh.truncate(keep)
        return size - keep


class VlState:
    """In-memory view rebuilt from vl.jsonl: recent validation keys and the last ledger."""

    def __init__(self, keep=KEEP_LEDGERS):
        self.keep = keep
        self.seen = {}  # ledger index -> set of (key, ledger hash)
        self.last_ledger = None
        self.last_t = None
        self.vals = 0
        self.bad_lines = 0

    def load(self, path):
        if not os.path.exists(path):
            return self
        with open(path) as fh:
            for line in fh:
                try:
                    e = json.loads(line)
                except ValueError:
                    self.bad_lines += 1
                    continue
                self.apply(e)
        return self

    def has(self, li, k, lh):
        return (k, lh) in self.seen.get(li, ())

    def apply(self, e):
        ev = e.get("ev")
        if "t" in e:
            self.last_t = max(self.last_t or 0, e["t"])
        if ev == "val":
            self.seen.setdefault(e["li"], set()).add((e["k"], e["lh"]))
            self.vals += 1
            self._prune(e["li"])
        elif ev == "ledger":
            self.last_ledger = max(self.last_ledger or 0, e["li"])
            self._prune(e["li"])

    def _prune(self, li):
        if len(self.seen) > self.keep * 2:
            floor = li - self.keep
            for old in [x for x in self.seen if x < floor]:
                del self.seen[old]


class Handler:
    """Turns stream messages into vl.jsonl events (pure logic; I/O goes through `emit`)."""

    def __init__(self, state, emit):
        self.s = state
        self.emit = emit
        self.gap_reason = "restart" if state.last_ledger else None
        self.dups = 0

    def _event(self, e):
        self.s.apply(e)
        self.emit(e)

    def on_connect(self, reconnect):
        if reconnect:
            self.gap_reason = "reconnect"

    def on_message(self, m, t):
        typ = m.get("type")
        if typ == "validationReceived":
            k, lh = m.get("validation_public_key"), m.get("ledger_hash")
            try:
                li = int(m["ledger_index"])
            except (KeyError, TypeError, ValueError):
                return
            if not k or not lh:
                return
            if self.s.has(li, k, lh):
                self.dups += 1
                return
            e = {"ev": "val", "k": k, "li": li, "lh": lh, "st": m.get("signing_time"), "t": round(t, 3)}
            if m.get("master_key"):
                e["mk"] = m["master_key"]
            if m.get("validated_hash"):
                e["vh"] = m["validated_hash"]
            e["full"] = bool(m.get("full"))
            self._event(e)
        elif typ == "ledgerClosed":
            idx = int(m["ledger_index"])
            last = self.s.last_ledger
            if last is not None and idx <= last:
                return  # already recorded (resume overlap)
            if last is not None and idx > last + 1:
                self._event(
                    {
                        "ev": "gap",
                        "from": last + 1,
                        "to": idx - 1,
                        "count": idx - 1 - last,
                        "reason": self.gap_reason or "stream_skip",
                        "t": round(t, 3),
                    }
                )
            self.gap_reason = None
            self._event(
                {
                    "ev": "ledger",
                    "li": idx,
                    "lh": m.get("ledger_hash"),
                    "ct": m.get("ledger_time"),
                    "txn": m.get("txn_count"),
                    "t": round(t, 3),
                }
            )


class Writer:
    def __init__(self, path):
        self.fh = open(path, "a")

    def __call__(self, e):
        self.fh.write(json.dumps(e, separators=(",", ":")) + "\n")
        self.fh.flush()
        if e["ev"] in ("ledger", "gap", "session", "stop"):
            os.fsync(self.fh.fileno())


def log(fh, msg):
    line = f"{iso(time.time())} {msg}"
    print(line, flush=True)
    if fh:
        fh.write(line + "\n")
        fh.flush()


def load_window(out, hours):
    p = os.path.join(out, "state.json")
    if os.path.exists(p):
        with open(p) as fh:
            return json.load(fh), True
    now = int(time.time())
    st = {"window_start": now, "window_end": now + int(hours * 3600)}
    with open(p + ".tmp", "w") as fh:
        json.dump(st, fh)
    os.replace(p + ".tmp", p)
    return st, False


def send(ws, cmd, **kw):
    if cmd not in ALLOWED_COMMANDS:
        raise ValueError(f"refusing command {cmd}")
    ws.send(json.dumps({"command": cmd, **kw}))


def run(args):
    global _stop
    os.makedirs(args.out, exist_ok=True)
    lf = open(args.log, "a") if args.log else None
    path = os.path.join(args.out, "vl.jsonl")
    torn = drop_torn_tail(path)
    state = VlState().load(path)
    win, resumed = load_window(args.out, args.hours)
    w = Writer(path)
    h = Handler(state, w)
    w(
        {
            "ev": "session",
            "start": round(time.time(), 3),
            "resumed": resumed,
            "last_ledger": state.last_ledger,
            "last_t": state.last_t,
            "torn_bytes": torn,
            "bad_lines": state.bad_lines,
            "window_end": win["window_end"],
        }
    )
    log(
        lf,
        f"{'resume' if resumed else 'start'} window {iso(win['window_start'])} -> {iso(win['window_end'])}"
        f" last_ledger={state.last_ledger} vals={state.vals} torn_bytes={torn}"
        f" bad_lines={state.bad_lines} pid={os.getpid()}",
    )
    if time.time() >= win["window_end"]:
        log(lf, "window already complete, nothing to do")
        return 0
    signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__("_stop", "SIGTERM"))
    signal.signal(signal.SIGINT, lambda *_: globals().__setitem__("_stop", "SIGINT"))
    connects, fails, next_hour = 0, 0, time.time() + 3600
    while _stop is None and time.time() < win["window_end"]:
        try:
            ws = WsClient(args.url, timeout=10)
            ws.settimeout(args.silence)
            h.on_connect(reconnect=connects > 0)
            connects += 1
            fails = 0
            send(ws, "subscribe", id="sub", streams=STREAMS)
            log(lf, f"connected ({connects}) {args.url}, subscribed {','.join(STREAMS)}")
            while _stop is None and time.time() < win["window_end"]:
                raw = ws.recv()
                t = time.time()
                m = json.loads(raw)
                if m.get("type") == "response":
                    if m.get("status") != "success":
                        log(lf, f"error response id={m.get('id')}: {m.get('error')}")
                    continue
                h.on_message(m, t)
                if t >= next_hour:
                    next_hour += 3600
                    log(
                        lf,
                        f"progress last_ledger={state.last_ledger} vals={state.vals} dups_dropped={h.dups}",
                    )
            ws.close()
        except (OSError, ConnectionError, ValueError, socket.timeout) as e:
            log(lf, f"stream error: {type(e).__name__}: {e}; reconnect in {args.retry}s")
            fails += 1
            if fails >= args.max_fail:
                # e.g. the container restarted: our network namespace is gone for good; let the
                # supervisor re-enter the new one. Window and data resume on the next start.
                w({"ev": "stop", "t": round(time.time(), 3), "reason": f"{fails} failed connects"})
                log(
                    lf, f"exit after {fails} failed connects (supervisor restarts with the current namespace)"
                )
                return 2
            for _ in range(args.retry):
                if _stop is not None:
                    break
                time.sleep(1)
    reason = _stop or "window complete"
    w({"ev": "stop", "t": round(time.time(), 3), "reason": reason})
    log(lf, f"stop ({reason}) last_ledger={state.last_ledger} vals={state.vals} dups_dropped={h.dups}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--url", default="ws://127.0.0.1:6006/")
    ap.add_argument("--out", required=True)
    ap.add_argument("--log")
    ap.add_argument("--hours", type=float, default=7)
    ap.add_argument("--silence", type=int, default=60, help="reconnect after N s without any message")
    ap.add_argument("--retry", type=int, default=10)
    ap.add_argument("--max-fail", type=int, default=3, help="exit 2 after N connection failures in a row")
    return run(ap.parse_args())


if __name__ == "__main__":
    sys.exit(main())
