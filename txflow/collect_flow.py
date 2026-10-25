#!/usr/bin/env python3
"""Resumable, read-only transaction-lifecycle collector (Python 3 stdlib only).

Subscribes to the `ledger`, `transactions` and `transactions_proposed` streams of your own
node's loopback admin WebSocket and sends one `fee` request per validated ledger. Nothing
else is ever sent (no submit, no sign). Run it inside the node container's network namespace,
because the admin port listens on the container's own loopback (see run-collector.sh).

Output: --out/flow.jsonl, append-only, one JSON object per line, `ev` is one of
  session    collector (re)start: {start, resumed, last_ledger, torn_bytes, bad_lines}
  ledger     validated ledger closed: {ledger_index, ledger_hash, ledger_time, txn_count, t}
  fee        fee RPC sampled right after that ledger: {ledger_index, t, queue, open_ledger_fee, ...}
  proposed   FIRST time this node saw a tx hash in transactions_proposed (validated=false):
             {hash, t, tx_type, fee, engine_result, ledger_current_index}
  validated  first validated message for a hash: {hash, t, tx_type, fee, ledger_index, result}
  gap        ledgers the ledger stream never delivered: {from, to, count, reason}
             reason = stream_skip (same connection) | reconnect | restart (collector was down)
  stop       clean stop: {t, reason}
`t` is this host's UTC clock in unix seconds when the message was read (single vantage point).

Window: state.json keeps {window_start, window_end}; a restart with the same --out resumes the
same window. Torn last line (kill during a write) is cut on start and counted in the session event.
"""

import argparse
import json
import os
import signal
import socket
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wsclient import WsClient  # noqa: E402

STREAMS = ["ledger", "transactions", "transactions_proposed"]
ALLOWED_COMMANDS = {"subscribe", "fee"}
_stop = None


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def drop_torn_tail(path):
    """Cut a partial last line. Returns the number of bytes removed."""
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return 0
    with open(path, "rb+") as fh:
        data = fh.read()
        if data.endswith(b"\n"):
            return 0
        keep = data.rfind(b"\n") + 1
        fh.truncate(keep)
        return len(data) - keep


class FlowState:
    """In-memory view rebuilt from flow.jsonl: seen hashes and the last validated ledger."""

    def __init__(self):
        self.proposed = set()
        self.validated = set()
        self.last_ledger = None
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

    def apply(self, e):
        ev = e.get("ev")
        if ev == "proposed":
            self.proposed.add(e["hash"])
        elif ev == "validated":
            self.validated.add(e["hash"])
        elif ev == "ledger":
            self.last_ledger = max(self.last_ledger or 0, e["ledger_index"])


class Handler:
    """Turns stream messages into flow.jsonl events (pure logic; I/O goes through `emit`)."""

    def __init__(self, state, emit):
        self.s = state
        self.emit = emit
        self.gap_reason = "restart" if state.last_ledger else None

    def _event(self, e):
        self.s.apply(e)
        self.emit(e)

    def on_connect(self, reconnect):
        if reconnect:
            self.gap_reason = "reconnect"

    def on_message(self, m, t):
        """Returns a ledger index when a fee sample should be requested for it, else None."""
        typ = m.get("type")
        if typ == "ledgerClosed":
            idx = int(m["ledger_index"])
            last = self.s.last_ledger
            if last is not None and idx <= last:
                return None  # already recorded (resume overlap)
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
                    "ledger_index": idx,
                    "ledger_hash": m.get("ledger_hash"),
                    "ledger_time": m.get("ledger_time"),
                    "txn_count": m.get("txn_count"),
                    "t": round(t, 3),
                }
            )
            return idx
        if typ == "transaction":
            tx = m.get("transaction") or {}
            h = tx.get("hash") or m.get("hash")
            if not h:
                return None
            base = {
                "hash": h,
                "t": round(t, 3),
                "tx_type": tx.get("TransactionType"),
                "fee": int(tx["Fee"]) if str(tx.get("Fee", "")).isdigit() else None,
            }
            if m.get("validated"):
                if h in self.s.validated:
                    return None  # same tx from the second stream
                meta = m.get("meta") or {}
                self._event(
                    {
                        "ev": "validated",
                        **base,
                        "ledger_index": m.get("ledger_index"),
                        "result": meta.get("TransactionResult") or m.get("engine_result"),
                    }
                )
            else:
                if h in self.s.proposed:
                    return None
                self._event(
                    {
                        "ev": "proposed",
                        **base,
                        "engine_result": m.get("engine_result"),
                        "ledger_current_index": m.get("ledger_current_index"),
                    }
                )
        return None

    def on_fee(self, idx, r, t):
        d = r.get("drops") or {}
        self._event(
            {
                "ev": "fee",
                "ledger_index": idx,
                "t": round(t, 3),
                "current_ledger_size": int(r.get("current_ledger_size", 0)),
                "expected_ledger_size": int(r.get("expected_ledger_size", 0)),
                "queue": int(r.get("current_queue_size", 0)),
                "max_queue": int(r.get("max_queue_size", 0)),
                "base_fee": int(d.get("base_fee", 0)),
                "minimum_fee": int(d.get("minimum_fee", 0)),
                "median_fee": int(d.get("median_fee", 0)),
                "open_ledger_fee": int(d.get("open_ledger_fee", 0)),
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
    st = {"window_start": now, "window_end": now + int(hours * 3600), "url": None}
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
    path = os.path.join(args.out, "flow.jsonl")
    torn = drop_torn_tail(path)
    state = FlowState().load(path)
    win, resumed = load_window(args.out, args.hours)
    w = Writer(path)
    h = Handler(state, w)
    w(
        {
            "ev": "session",
            "start": round(time.time(), 3),
            "resumed": resumed,
            "last_ledger": state.last_ledger,
            "torn_bytes": torn,
            "bad_lines": state.bad_lines,
            "window_end": win["window_end"],
        }
    )
    log(
        lf,
        f"{'resume' if resumed else 'start'} window {iso(win['window_start'])} -> {iso(win['window_end'])}"
        f" last_ledger={state.last_ledger} proposed={len(state.proposed)} validated={len(state.validated)}"
        f" torn_bytes={torn} bad_lines={state.bad_lines} pid={os.getpid()}",
    )
    if time.time() >= win["window_end"]:
        log(lf, "window already complete, nothing to do")
        return 0
    signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__("_stop", "SIGTERM"))
    signal.signal(signal.SIGINT, lambda *_: globals().__setitem__("_stop", "SIGINT"))
    connects, fails, next_hour, fee_id = 0, 0, time.time() + 3600, 0
    pending = {}
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
                    if m.get("id") in pending and m.get("status") == "success":
                        h.on_fee(pending.pop(m["id"]), m["result"], t)
                    elif m.get("status") != "success":
                        log(lf, f"error response id={m.get('id')}: {m.get('error')}")
                        pending.pop(m.get("id"), None)
                    continue
                idx = h.on_message(m, t)
                if idx is not None:
                    fee_id += 1
                    pending[f"fee{fee_id}"] = idx
                    send(ws, "fee", id=f"fee{fee_id}")
                if t >= next_hour:
                    next_hour += 3600
                    log(
                        lf,
                        f"progress last_ledger={state.last_ledger} proposed={len(state.proposed)}"
                        f" validated={len(state.validated)}",
                    )
            ws.close()
        except (OSError, ConnectionError, ValueError, socket.timeout) as e:
            log(lf, f"stream error: {type(e).__name__}: {e}; reconnect in {args.retry}s")
            pending.clear()
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
    log(
        lf,
        f"stop ({reason}) last_ledger={state.last_ledger} proposed={len(state.proposed)}"
        f" validated={len(state.validated)}",
    )
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--url", default="ws://127.0.0.1:6006/")
    ap.add_argument("--out", required=True)
    ap.add_argument("--log")
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--silence", type=int, default=60, help="reconnect after N s without any message")
    ap.add_argument("--retry", type=int, default=10)
    ap.add_argument("--max-fail", type=int, default=3, help="exit 2 after N connection failures in a row")
    return run(ap.parse_args())


if __name__ == "__main__":
    sys.exit(main())
