#!/usr/bin/env python3
"""Resumable, read-only ledger-state snapshot of ONE validated ledger (Python 3 stdlib only).

Pages the full state tree of one validated ledger with `ledger_data` (binary=false,
marker-based) from your own node's admin JSON-RPC through `docker exec <container> curl`
(the admin port is never exposed), then writes a deterministic gzip snapshot.

Files in --out:
  state.json       ledger_index + ledger_hash pinned on the first start, ledger header
                   (total_coins, close_time) and server_state reserves at that time
  pages.jsonl      one line per fetched page: page no, marker in/out, object list.
                   This is the resume source of truth: each line is flushed + fsynced
                   before the next request, a torn last line (SIGKILL) is cut on restart.
  snapshot-<ledger_index>.jsonl.gz
                   written when the last page (no marker) has arrived: line 1 = header
                   (ledger fields of state.json + objects_paged), then one object per line
                   in paging order. gzip mtime=0 and sorted JSON keys, so the same ledger
                   always gives the same bytes, whatever the page size or interruptions.
  snapshot.log     (--log) start/resume/repair/page progress/done

A restart with the same --out resumes from the last complete page's marker, on the same
pinned ledger. If the node has meanwhile deleted that ledger (online_delete), the run stops
with exit 3 instead of mixing ledgers.

Only read-only RPC methods are sent: server_state, ledger, ledger_data (account_info is
allowed for crosscheck_state.py).

Usage:
  ./snapshot.py --out data/state [--ledger N] [--limit 2048] [--page-delay 0]
"""

import argparse
import gzip
import io
import json
import os
import platform
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

READ_ONLY_METHODS = {"server_state", "ledger", "ledger_data", "account_info"}
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
    """docker exec JSON-RPC client with a read-only allowlist and a request-rate limiter."""

    def __init__(self, container="postfiatd", port=5005, rate=2.0, docker_cmd=("sudo", "docker")):
        self.container = container
        self.url = f"http://127.0.0.1:{port}/"
        self.min_gap = 1.0 / rate
        self.docker_cmd = list(docker_cmd)
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
            "exec", self.container, "curl", "-s", "-m", "30", "-X", "POST",
            "-H", "Content-Type: application/json", "-d", body, self.url,
        ]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            raise RuntimeError(f"docker exec rc={out.returncode}: {out.stderr.strip()[:200]}")
        result = json.loads(out.stdout)["result"]
        if result.get("status") == "error":
            raise LookupError(result.get("error", "unknown"))
        return result


def repair_pages(path):
    """Cut a torn (unterminated or unparsable) last line. Returns (pages, bytes_cut)."""
    if not os.path.exists(path):
        return [], 0
    with open(path, "rb") as fh:
        data = fh.read()
    pages, good, pos = [], 0, 0
    while True:
        nl = data.find(b"\n", pos)
        if nl < 0:
            break  # unterminated tail = torn
        try:
            pages.append(json.loads(data[pos:nl]))
        except json.JSONDecodeError:
            break  # treat everything from here as torn
        pos = good = nl + 1
    cut = len(data) - good
    if cut:
        with open(path, "r+b") as fh:
            fh.truncate(good)
    return pages, cut


def check_pages(pages, state):
    """Pages must be numbered 0..n-1, chained by marker, and all on the pinned ledger."""
    prev = None
    for i, p in enumerate(pages):
        if p["page"] != i:
            raise ValueError(f"page numbering broken at line {i}: page={p['page']}")
        if p["ledger_index"] != state["ledger_index"] or p["ledger_hash"] != state["ledger_hash"]:
            raise ValueError(f"page {i} is from another ledger ({p['ledger_index']})")
        if p["marker_in"] != prev:
            raise ValueError(f"page {i} marker_in does not continue page {i - 1}")
        prev = p["marker_out"]


def write_atomic(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def pin_ledger(rpc, ledger):
    """Resolve the ledger to snapshot and record header + reserves."""
    ss = rpc.call("server_state")["state"]
    vl = ss["validated_ledger"]
    idx = int(ledger) if ledger else int(vl["seq"])
    hdr = rpc.call("ledger", {"ledger_index": idx})["ledger"]
    if not hdr.get("closed", True) or str(hdr["ledger_index"]) != str(idx):
        raise RuntimeError(f"ledger {idx} not closed/validated")
    return {
        "ledger_index": idx,
        "ledger_hash": hdr["ledger_hash"],
        "account_hash": hdr["account_hash"],
        "total_coins": int(hdr["total_coins"]),
        "close_time": int(hdr["close_time"]),
        "close_time_iso": datetime.fromtimestamp(int(hdr["close_time"]) + RIPPLE_EPOCH, timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "server_state_reserve_base": int(vl["reserve_base"]),
        "server_state_reserve_inc": int(vl["reserve_inc"]),
        "server_state_validated_seq": int(vl["seq"]),
    }


def write_snapshot(out, state, pages):
    objs = [o for p in pages for o in p["state"]]
    # header = ledger facts only: no page count or pin-time validated seq, so the same ledger gives
    # the same bytes whatever the page size, interruptions or time of the run
    header = {k: v for k, v in state.items() if k != "server_state_validated_seq"}
    header.update(objects_paged=len(objs), format="pft-state-snapshot/1")
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0, filename="") as gz:
        gz.write((json.dumps(header, sort_keys=True, separators=(",", ":")) + "\n").encode())
        for o in objs:
            gz.write((json.dumps(o, sort_keys=True, separators=(",", ":")) + "\n").encode())
    path = os.path.join(out, f"snapshot-{state['ledger_index']}.jsonl.gz")
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(buf.getvalue())
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return path, len(objs)


def collect(rpc, out, ledger=None, limit=2048, page_delay=0.0, logfh=None, max_pages=None):
    """Run or resume a snapshot. Returns (snapshot_path or None, objects). Raises on errors."""
    os.makedirs(out, exist_ok=True)
    spath = os.path.join(out, "state.json")
    ppath = os.path.join(out, "pages.jsonl")
    if os.path.exists(spath):
        with open(spath) as fh:
            state = json.load(fh)
        if ledger and int(ledger) != state["ledger_index"]:
            raise ValueError(f"--ledger {ledger} != pinned {state['ledger_index']} in {spath}")
        pages, cut = repair_pages(ppath)
        if cut:
            log(logfh, f"REPAIR cut {cut} bytes of torn last line from pages.jsonl")
        check_pages(pages, state)
        n_obj = sum(len(p["state"]) for p in pages)
        log(logfh, f"RESUME ledger {state['ledger_index']} {state['ledger_hash']} after page "
                   f"{len(pages) - 1} ({n_obj} objects), marker "
                   f"{(pages[-1]['marker_out'] if pages else None) or '-'}")
    else:
        state = pin_ledger(rpc, ledger)
        write_atomic(spath, json.dumps(state, sort_keys=True, indent=1) + "\n")
        pages = []
        if os.path.exists(ppath):
            os.remove(ppath)
        log(logfh, f"START ledger {state['ledger_index']} {state['ledger_hash']} total_coins "
                   f"{state['total_coins']} close {state['close_time_iso']} limit {limit} | "
                   f"host python {platform.python_version()}")

    if pages and pages[-1]["marker_out"] is None:
        path, n = write_snapshot(out, state, pages)
        log(logfh, f"DONE (already complete) {n} objects in {len(pages)} pages -> {os.path.basename(path)}")
        return path, n

    marker = pages[-1]["marker_out"] if pages else None
    fetched = 0
    with open(ppath, "a") as pf:
        while not _stop:
            params = {"ledger_index": state["ledger_index"], "binary": False, "limit": limit}
            if marker:
                params["marker"] = marker
            try:
                r = rpc.call("ledger_data", params)
            except LookupError as e:
                if str(e) == "lgrNotFound":
                    log(logfh, f"ABORT ledger {state['ledger_index']} no longer on this node (lgrNotFound)")
                    raise SystemExit(3) from None
                raise
            if r["ledger_hash"] != state["ledger_hash"]:
                raise RuntimeError(f"node returned ledger hash {r['ledger_hash']} != pinned")
            page = {
                "page": len(pages), "ledger_index": state["ledger_index"],
                "ledger_hash": state["ledger_hash"], "marker_in": marker,
                "marker_out": r.get("marker"), "state": r["state"],
            }
            pf.write(json.dumps(page, sort_keys=True, separators=(",", ":")) + "\n")
            pf.flush()
            os.fsync(pf.fileno())
            pages.append(page)
            fetched += 1
            marker = page["marker_out"]
            n_obj = sum(len(p["state"]) for p in pages)
            log(logfh, f"page {page['page']} +{len(r['state'])} objects (total {n_obj}) "
                       f"marker {(marker or '-')[:16]}")
            if marker is None:
                break
            if max_pages and fetched >= max_pages:
                return None, n_obj
            if page_delay:
                time.sleep(page_delay)
    if _stop:
        log(logfh, f"STOP by signal after page {len(pages) - 1}; restart with the same command to resume")
        return None, sum(len(p["state"]) for p in pages)
    path, n = write_snapshot(out, state, pages)
    log(logfh, f"DONE {n} objects in {len(pages)} pages, {rpc.count} RPC calls this run -> "
               f"{os.path.basename(path)}")
    return path, n


def _on_signal(signum, _frame):
    global _stop
    _stop = True


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="data/state")
    p.add_argument("--ledger", type=int, default=None, help="ledger index (default: current validated)")
    p.add_argument("--limit", type=int, default=2048, help="objects per ledger_data page")
    p.add_argument("--page-delay", type=float, default=0.0, help="extra sleep between pages (drills)")
    p.add_argument("--rate", type=float, default=2.0, help="max RPC requests per second")
    p.add_argument("--log", default=None)
    p.add_argument("--container", default="postfiatd")
    p.add_argument("--port", type=int, default=5005)
    p.add_argument("--no-sudo", action="store_true")
    args = p.parse_args(argv)
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    rpc = Rpc(args.container, args.port, args.rate, ["docker"] if args.no_sudo else ["sudo", "docker"])
    logfh = open(args.log, "a") if args.log else None
    try:
        path, _ = collect(rpc, args.out, args.ledger, args.limit, args.page_delay, logfh)
    finally:
        if logfh:
            logfh.close()
    return 0 if path else 4


if __name__ == "__main__":
    sys.exit(main())
