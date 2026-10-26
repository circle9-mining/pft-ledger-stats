#!/usr/bin/env python3
"""Resumable, read-only amendment/fee vote collector (Python 3 stdlib only).

Subscribes to the `validations` and `ledger` streams of your own node's loopback admin WebSocket
and, at every flag ledger, reads the Amendments and FeeSettings ledger objects with `ledger_entry`.
Nothing else is ever sent (no submit, no sign). Run it inside the node container's network
namespace (run-collector.sh), like vlatency/.

Voting: validators put their amendment and fee votes in the validations of the ledger BEFORE a
flag ledger, i.e. ledger index li with (li + 1) % 256 == 0 (a "voting ledger"). For those ledgers
every validation is recorded with its vote fields, including validations that carry none, so the
analyzer can tell "validated the voting ledger, voted for nothing" from "no validation seen".

Output: --out/govern.jsonl, append-only, one JSON object per line, `ev` is one of
  session  collector (re)start: {start, resumed, last_ledger, last_t, torn_bytes, bad_lines, window_end}
  ledger   validated ledger closed: {li, lh, ct, t}
  vote     validation of a voting ledger: {k, mk, li, lh, full, sv, am, fee, extra, t}
           am  = list of amendment IDs voted for (empty list if the field is absent)
           fee = {field: value} for the fee-vote fields present (empty dict if none)
           sv  = server_version as sent; extra = sorted field names outside the known set (new shapes)
  snap     ledger objects read at a flag ledger: {li, obj: "Amendments"|"FeeSettings", node | error, t}
  gap      ledgers the ledger stream never delivered: {from, to, count, reason, t}
  stop     clean stop: {t, reason}
`t` is this host's UTC clock in unix seconds when the message was read.

Window, torn-tail repair and resume work exactly as in vlatency/collect_vlatency.py (state.json
keeps {window_start, window_end}; a torn last line is cut on start and counted in the session event).
"""

import argparse
import json
import os
import signal
import socket
import sys
import time

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(here, "..", "txflow"))
sys.path.insert(0, os.path.join(here, "..", "vlatency"))
from collect_vlatency import drop_torn_tail, iso, load_window, log  # noqa: E402
from wsclient import WsClient  # noqa: E402

STREAMS = ["validations", "ledger"]
ALLOWED_COMMANDS = {"subscribe", "ledger_entry"}
# well-known singleton ledger object IDs (same on every XRPL-derived network)
OBJECTS = {
    "Amendments": "7DB0788C020F02780A673DC74757F23823FA3014C1866E72CC4CD8B226CD6EF4",
    "FeeSettings": "4BC50C9B0D8515D3EAAE1E74B29A95804346C491EE1A95BF25E4AAB854A6A651",
}
FEE_FIELDS = (
    "base_fee",
    "reserve_base",
    "reserve_inc",
    "base_fee_drops",
    "reserve_base_drops",
    "reserve_increment_drops",
    "load_fee",
)
# every validationReceived field seen in the 2026-10-26 probe, plus the vote fields
KNOWN = {
    "type",
    "cookie",
    "data",
    "flags",
    "full",
    "ledger_hash",
    "ledger_index",
    "master_key",
    "network_id",
    "server_version",
    "signature",
    "signing_time",
    "validated_hash",
    "validation_public_key",
    "amendments",
    *FEE_FIELDS,
}
_stop = None


def is_voting(li):
    return (li + 1) % 256 == 0


def is_flag(li):
    return li % 256 == 0


class GovState:
    """In-memory view rebuilt from govern.jsonl: votes already written, snapshots taken, last ledger."""

    def __init__(self):
        self.votes = set()  # (li, k, lh)
        self.snaps = set()  # (li, obj)
        self.last_ledger = None
        self.last_t = None
        self.bad_lines = 0

    def load(self, path):
        if os.path.exists(path):
            with open(path) as fh:
                for line in fh:
                    try:
                        self.apply(json.loads(line))
                    except (ValueError, KeyError, TypeError):
                        self.bad_lines += 1
        return self

    def apply(self, e):
        ev = e.get("ev")
        if "t" in e:
            self.last_t = max(self.last_t or 0, e["t"])
        if ev == "vote":
            self.votes.add((e["li"], e["k"], e["lh"]))
        elif ev == "snap" and "node" in e:
            self.snaps.add((e["li"], e["obj"]))
        elif ev == "ledger":
            self.last_ledger = max(self.last_ledger or 0, e["li"])


class Handler:
    """Stream messages -> govern.jsonl events (pure logic: output via `emit`, requests via `request`)."""

    def __init__(self, state, emit, request):
        self.s, self.emit, self.request = state, emit, request
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
            try:
                li = int(m["ledger_index"])
            except (KeyError, TypeError, ValueError):
                return
            k, lh = m.get("validation_public_key"), m.get("ledger_hash")
            if not k or not lh or not is_voting(li):
                return
            if (li, k, lh) in self.s.votes:
                self.dups += 1
                return
            self._event(
                {
                    "ev": "vote",
                    "k": k,
                    "mk": m.get("master_key"),
                    "li": li,
                    "lh": lh,
                    "full": bool(m.get("full")),
                    "sv": m.get("server_version"),
                    "am": list(m.get("amendments") or []),
                    "fee": {f: m[f] for f in FEE_FIELDS if f in m},
                    "extra": sorted(set(m) - KNOWN),
                    "t": round(t, 3),
                }
            )
        elif typ == "ledgerClosed":
            idx = int(m["ledger_index"])
            last = self.s.last_ledger
            if last is not None and idx <= last:
                return  # resume overlap
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
                    "t": round(t, 3),
                }
            )
            if is_flag(idx):
                for obj, oid in OBJECTS.items():
                    if (idx, obj) not in self.s.snaps:
                        self.request("ledger_entry", id=f"snap:{idx}:{obj}", index=oid, ledger_index=idx)

    def on_response(self, m, t):
        rid = str(m.get("id") or "")
        if not rid.startswith("snap:"):
            return False
        _, li, obj = rid.split(":", 2)
        e = {"ev": "snap", "li": int(li), "obj": obj, "t": round(t, 3)}
        if m.get("status") == "success" and "node" in (m.get("result") or {}):
            e["node"] = m["result"]["node"]
        else:
            e["error"] = (m.get("result") or {}).get("error") or m.get("error") or "unknown"
        self._event(e)
        return True


class Writer:
    def __init__(self, path):
        self.fh = open(path, "a")

    def __call__(self, e):
        self.fh.write(json.dumps(e, separators=(",", ":")) + "\n")
        self.fh.flush()
        if e["ev"] in ("ledger", "snap", "gap", "session", "stop"):
            os.fsync(self.fh.fileno())


def send(ws, cmd, **kw):
    if cmd not in ALLOWED_COMMANDS:
        raise ValueError(f"refusing command {cmd}")
    ws.send(json.dumps({"command": cmd, **kw}))


def run(args):
    global _stop
    os.makedirs(args.out, exist_ok=True)
    lf = open(args.log, "a") if args.log else None
    path = os.path.join(args.out, "govern.jsonl")
    torn = drop_torn_tail(path)
    state = GovState().load(path)
    win, resumed = load_window(args.out, args.hours)
    w = Writer(path)
    ws_box = [None]
    h = Handler(state, w, lambda cmd, **kw: send(ws_box[0], cmd, **kw))
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
        f" last_ledger={state.last_ledger} votes={len(state.votes)} snaps={len(state.snaps)}"
        f" torn_bytes={torn}"
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
            ws = ws_box[0] = WsClient(args.url, timeout=10)
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
                    if not h.on_response(m, t) and m.get("status") != "success":
                        log(lf, f"error response id={m.get('id')}: {m.get('error')}")
                    continue
                h.on_message(m, t)
                if t >= next_hour:
                    next_hour += 3600
                    log(
                        lf,
                        f"progress last_ledger={state.last_ledger} votes={len(state.votes)}"
                        f" snaps={len(state.snaps)} dups_dropped={h.dups}",
                    )
            ws.close()
        except (OSError, ConnectionError, ValueError, socket.timeout) as e:
            log(lf, f"stream error: {type(e).__name__}: {e}; reconnect in {args.retry}s")
            fails += 1
            if fails >= args.max_fail:
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
        f"stop ({reason}) last_ledger={state.last_ledger} votes={len(state.votes)} snaps={len(state.snaps)}"
        f" dups_dropped={h.dups}",
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
