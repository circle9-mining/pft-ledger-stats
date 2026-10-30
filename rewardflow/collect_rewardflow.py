#!/usr/bin/env python3
"""Resumable, read-only account_tx collector for one account over a public Post Fiat RPC (stdlib only).

Walks `account_tx` forward (oldest first) with marker pagination across the server's whole
complete_ledgers range, and appends one JSON line per validated transaction to --out:

  {"kind": "range", ...}  first line: account, ledger_min/ledger_max pinned at the first start
  {"kind": "tx", ...}     ledger_index, close_time, hash, type, account, destination, amount,
                          delivered_amount, result, flags, fee, memos (decoded type/format),
                          direction (out/in/other) and meta_balance_delta (the account's own
                          AccountRoot Balance change from tx metadata, drops; null if untouched)
  {"kind": "done", ...}   ledger range the server reported as covered, tx and page counts

Resume: on restart with the same --out the pinned range is reused, a torn last line is cut off,
and paging restarts at the last stored ledger (inclusive); hashes already stored are skipped, so
a restart never writes duplicates. RPC errors and slowDown/tooBusy are retried with exponential
backoff. Only read-only methods are sent: server_info, account_tx (crosscheck: tx, account_info).

Usage: collect_rewardflow.py ACCOUNT --out data.jsonl [--rpc URL] [--limit 400] [--gap 0.5]
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

RIPPLE_EPOCH = 946684800  # 2000-01-01T00:00:00Z
READ_ONLY_METHODS = {"server_info", "account_tx", "tx", "account_info"}
MEMO_KIND_RE = re.compile(r"([a-z]+)_[0-9a-f]")
RETRY_ERRORS = {"slowDown", "tooBusy", "noNetwork", "noCurrent", "noClosed"}


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ripple_iso(t):
    if t is None:
        return None
    return datetime.fromtimestamp(int(t) + RIPPLE_EPOCH, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg):
    print(f"{utcnow()} {msg}", flush=True)


class RpcError(Exception):
    pass


class Rpc:
    """JSON-RPC over HTTPS with a minimum gap between requests and retry/backoff."""

    def __init__(self, url, gap=0.5, retries=6, backoff=2.0, post=None, sleep=time.sleep,
                 retry_errors=None):
        self.url, self.gap, self.retries, self.backoff = url, gap, retries, backoff
        self.retry_errors = RETRY_ERRORS if retry_errors is None else set(retry_errors)
        self.retry_kinds = {}  # retried error -> count; transport errors count as "transport"
        self.post = post or self._post
        self.sleep = sleep
        self.last = None
        self.calls = 0
        self.retried = 0

    def _post(self, body):
        req = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)

    def call(self, method, params):
        if method not in READ_ONLY_METHODS:
            raise ValueError(f"refusing non read-only method {method}")
        body = json.dumps({"method": method, "params": [params]}).encode()
        delay = self.backoff
        for attempt in range(1, self.retries + 1):
            if self.last is not None:
                self.sleep(self.gap)
            self.last = True
            self.calls += 1
            try:
                res = self.post(body)["result"]
            except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError) as ex:
                err = f"{type(ex).__name__}: {ex}"
                kind = "transport"
            else:
                if res.get("status") == "success" or "error" not in res:
                    return res
                err = res.get("error")
                kind = err
                if err not in self.retry_errors:
                    raise RpcError(f"{method}: {err}: {res.get('error_message', '')}")
            if attempt == self.retries:
                raise RpcError(f"{method}: giving up after {attempt} attempts: {err}")
            self.retried += 1
            self.retry_kinds[kind] = self.retry_kinds.get(kind, 0) + 1
            log(f"RETRY {method} attempt {attempt}/{self.retries} error={err} backoff={delay:.1f}s")
            self.sleep(delay)
            delay *= 2


RECORD_FIELDS = ["ledger_index", "close_time", "hash", "type", "account", "destination", "amount",
                 "delivered_amount", "result", "flags", "fee", "memos", "direction", "meta_balance_delta"]


def native_drops(v):
    return int(v) if isinstance(v, str) and v.isdigit() else None


def hex_text(h):
    if not h:
        return None
    try:
        return bytes.fromhex(h).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return "hex:" + h[:16]


def memo_kind(data_hex):
    """Coarse memo category: <word> of the first `<word>_<hex id>` token in MemoData (e.g. task, airdrop)."""
    if not data_hex:
        return None
    try:
        raw = bytes.fromhex(data_hex)
    except ValueError:
        return "undecodable"
    m = MEMO_KIND_RE.search(raw.decode("latin-1"))
    return m.group(1) if m else "other"


def balance_delta(meta, account):
    for n in meta.get("AffectedNodes", []):
        node = n.get("ModifiedNode") or n.get("CreatedNode") or n.get("DeletedNode") or {}
        if node.get("LedgerEntryType") != "AccountRoot":
            continue
        final = node.get("FinalFields") or node.get("NewFields") or {}
        if final.get("Account") != account:
            continue
        prev = (node.get("PreviousFields") or {}).get("Balance")
        if prev is None or "Balance" not in final:
            return 0
        return int(final["Balance"]) - int(prev)
    return None


def record(entry, account):
    tx = entry.get("tx") or entry.get("tx_json") or {}
    meta = entry.get("meta") or {}
    h = tx.get("hash") or entry.get("hash")
    li = tx.get("ledger_index") or entry.get("ledger_index")
    if tx.get("Account") == account:
        direction = "out"
    elif tx.get("Destination") == account:
        direction = "in"
    else:
        direction = "other"
    memos = []
    for m in tx.get("Memos") or []:
        m = m.get("Memo", {})
        memos.append({"type": hex_text(m.get("MemoType")), "format": hex_text(m.get("MemoFormat")),
                      "kind": memo_kind(m.get("MemoData"))})
    return {
        "kind": "tx", "ledger_index": li, "close_time": ripple_iso(tx.get("date")), "hash": h,
        "type": tx.get("TransactionType"), "account": tx.get("Account"), "destination": tx.get("Destination"),
        "amount": tx.get("Amount", tx.get("DeliverMax")), "delivered_amount": meta.get("delivered_amount"),
        "result": meta.get("TransactionResult"), "flags": tx.get("Flags", 0), "fee": tx.get("Fee"),
        "memos": memos, "direction": direction, "meta_balance_delta": balance_delta(meta, account),
        "validated": entry.get("validated"),
    }


def load_existing(path):
    """Return (range_line, done_line, hashes, last_ledger, n_tx); cut a torn last line in place."""
    if not os.path.exists(path):
        return None, None, set(), None, 0
    with open(path, "rb") as fh:
        data = fh.read()
    if data and not data.endswith(b"\n"):
        cut = data.rfind(b"\n") + 1
        log(f"RESUME torn last line ({len(data) - cut} bytes) removed")
        with open(path, "r+b") as fh:
            fh.truncate(cut)
        data = data[:cut]
    rng = done = last = None
    hashes = set()
    for line in data.decode().splitlines():
        e = json.loads(line)
        if e["kind"] == "range":
            rng = e
        elif e["kind"] == "done":
            done = e
        elif e["kind"] == "tx":
            hashes.add(e["hash"])
            last = e["ledger_index"]
    return rng, done, hashes, last, len(hashes)


def append(fh, obj):
    fh.write(json.dumps(obj, sort_keys=True) + "\n")
    fh.flush()
    os.fsync(fh.fileno())


def collect(account, out, rpc, limit=400, max_pages=None):
    rng, done, hashes, last, n = load_existing(out)
    if done:
        log(f"DONE already: {out} covers {done['ledger_min']}-{done['ledger_max']} ({done['tx_count']} tx)")
        return done
    with open(out, "a") as fh:
        if rng is None:
            info = rpc.call("server_info", {})["info"]
            lo, hi = (int(x) for x in info["complete_ledgers"].split(",")[-1].split("-"))
            rng = {"kind": "range", "account": account, "ledger_min": lo, "ledger_max": hi,
                   "complete_ledgers": info["complete_ledgers"], "rpc": rpc.url, "started": utcnow()}
            append(fh, rng)
            log(f"START account={account} range {lo}-{hi}"
                f" (server complete_ledgers {info['complete_ledgers']})")
        else:
            log(f"RESUME {n} tx stored, last ledger {last},"
                f" pinned range {rng['ledger_min']}-{rng['ledger_max']}")
        start = last if last is not None else rng["ledger_min"]
        marker, pages, new, skipped, covered = None, 0, 0, 0, None
        while True:
            p = {"account": account, "ledger_index_min": start, "ledger_index_max": rng["ledger_max"],
                 "limit": limit, "forward": True}
            if marker:
                p["marker"] = marker
            res = rpc.call("account_tx", p)
            pages += 1
            if covered is None:
                covered = res.get("ledger_index_min")
            for entry in res.get("transactions", []):
                r = record(entry, account)
                if not r["validated"]:
                    continue
                if r["hash"] in hashes:
                    skipped += 1
                    continue
                hashes.add(r["hash"])
                append(fh, r)
                new += 1
            marker = res.get("marker")
            log(f"PAGE {pages} +{len(res.get('transactions', []))} tx (new {new}, dup-skipped {skipped})"
                f" marker={'yes' if marker else 'none'}")
            if not marker:
                break
            if max_pages and pages >= max_pages:
                log(f"STOP after --max-pages {max_pages} (resume with the same command)")
                return None
        done = {"kind": "done", "ledger_min": rng["ledger_min"], "ledger_max": rng["ledger_max"],
                "server_ledger_index_min_this_run": covered,
                "server_ledger_index_max_this_run": res.get("ledger_index_max"),
                "tx_count": len(hashes), "pages_this_run": pages, "dup_skipped_this_run": skipped,
                "rpc_calls": rpc.calls, "rpc_retries": rpc.retried, "finished": utcnow()}
        append(fh, done)
        log(f"DONE {done['tx_count']} tx over ledgers {done['ledger_min']}-{done['ledger_max']}"
            f" pages={pages} rpc_calls={rpc.calls} retries={rpc.retried}")
        return done


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("account")
    ap.add_argument("--out", required=True)
    ap.add_argument("--rpc", default="https://rpc.testnet.postfiat.org")
    ap.add_argument("--limit", type=int, default=400)
    ap.add_argument("--gap", type=float, default=0.5, help="seconds between requests")
    ap.add_argument("--max-pages", type=int, default=None, help="stop early (for resume drills)")
    a = ap.parse_args(argv)
    try:
        collect(a.account, a.out, Rpc(a.rpc, gap=a.gap), limit=a.limit, max_pages=a.max_pages)
    except RpcError as ex:
        log(f"ERROR {ex}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
