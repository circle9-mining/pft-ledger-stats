#!/usr/bin/env python3
"""Resumable, read-only NegativeUNL history collector (Python 3 stdlib only).

Walks every flag ledger F (F % 256 == 0) in a ledger range of a JSON-RPC server (default: the
public testnet RPC, range = its whole complete_ledgers) and, for each F, records:
  * the UNLModify pseudo-transactions in F (postfiatd puts them only in flag ledgers:
    Change.cpp applyUNLModify rejects any other ledger; RCLConsensus.cpp adds them in the
    consensus round that follows a voting ledger),
  * the NegativeUNL ledger object at F (DisabledValidators + pending ValidatorToDisable /
    ValidatorToReEnable; Ledger::updateNegativeUNL moves pending entries into the list when the
    NEXT flag ledger is built, BuildLedger.cpp),
  * the NegativeUNL object at F+1 (the following ledger), so the analyzer can prove the object
    only ever changes at flag ledgers.
Only read-only methods are sent: server_info, ledger, ledger_entry.

Output: --out/nunl.jsonl, append-only, one JSON object per line, `ev` is one of
  session  collector (re)start: {start, rpc, complete_ledgers, from, to, have, torn_bytes, bad_lines}
  flag     one flag ledger: {li, lh, ct, ntx, mods, nunl, next}
           mods = [{validator, disabling, seq, hash}] (UNLModify txs in F, in ledger order)
           nunl / next = {"disabled": [keys], "to_disable": key|null, "to_reenable": key|null}
                         or null when the object does not exist (entryNotFound) at F / F+1
  error    a flag ledger that failed after all retries: {li, error} (retried on the next run)
  stop     {t, reason, done, errors, calls, retried}
`ct` is the ledger close_time (Ripple epoch seconds) as the server sent it.

Resume: flag ledgers already in the file (ev=flag) are skipped; a torn last line (kill during a
write) is cut on start and counted in the session event. Each line is fsynced.
"""

import argparse
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

FLAG = 256
NUNL_INDEX = "2E8A59AA9D3B5B186B0B9E0F62E6C02587CA74A4D778938E957B6357D364B244"  # NegativeUNL singleton
READ_ONLY_METHODS = {"server_info", "ledger", "ledger_entry"}
# answers that a busy/lagging public server gives transiently (seen in rewardflow/govern runs)
RETRY_ERRORS = {"lgrNotFound", "internal", "tooBusy", "slowDown", "noNetwork", "notReady", "noCurrent"}
_stop = None


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg, fh=None):
    line = f"{utcnow()} {msg}"
    print(line, flush=True)
    if fh:
        fh.write(line + "\n")
        fh.flush()


class RpcError(Exception):
    pass


class Rpc:
    """JSON-RPC with a minimum gap between requests and logged exponential backoff retries."""

    def __init__(self, url, gap=0.2, retries=6, backoff=2.0, post=None, sleep=time.sleep, logf=None):
        self.url, self.gap, self.retries, self.backoff = url, gap, retries, backoff
        self.post = post or self._post
        self.sleep = sleep
        self.logf = logf
        self.calls = 0
        self.retried = 0

    def _post(self, body):
        req = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)

    def call(self, method, params, absent_ok=False):
        """Return the result dict, or None when absent_ok and the server says entryNotFound."""
        if method not in READ_ONLY_METHODS:
            raise ValueError(f"refusing non read-only method {method}")
        body = json.dumps({"method": method, "params": [params]}).encode()
        delay = self.backoff
        for attempt in range(1, self.retries + 1):
            if self.calls:
                self.sleep(self.gap)
            self.calls += 1
            try:
                res = self.post(body)["result"]
            except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError) as ex:
                err = f"{type(ex).__name__}: {ex}"
            else:
                err = res.get("error")
                if err is None:
                    return res
                if err == "entryNotFound" and absent_ok:
                    return None
                if err not in RETRY_ERRORS:
                    raise RpcError(f"{method}: {err}: {res.get('error_message', '')}")
            if attempt == self.retries:
                raise RpcError(f"{method}: giving up after {attempt} attempts: {err}")
            self.retried += 1
            log(
                f"RETRY {method} {params.get('ledger_index')} attempt {attempt}/{self.retries} "
                f"error={err} backoff={delay:.1f}s",
                self.logf,
            )
            self.sleep(delay)
            delay *= 2


def nunl_view(node):
    """NegativeUNL ledger object -> {disabled, to_disable, to_reenable}; None if absent."""
    if node is None:
        return None
    disabled = [d["DisabledValidator"]["PublicKey"] for d in node.get("DisabledValidators", [])]
    return {
        "disabled": sorted(disabled),
        "to_disable": node.get("ValidatorToDisable"),
        "to_reenable": node.get("ValidatorToReEnable"),
    }


def unl_mods(txs):
    out = []
    for tx in txs:
        if tx.get("TransactionType") != "UNLModify":
            continue
        out.append(
            {
                "validator": tx.get("UNLModifyValidator"),
                "disabling": tx.get("UNLModifyDisabling"),
                "seq": tx.get("LedgerSequence"),
                "hash": tx.get("hash"),
            }
        )
    return out


def fetch_flag(rpc, li):
    led = rpc.call("ledger", {"ledger_index": li, "transactions": True, "expand": True})
    lg = led.get("ledger") or {}
    if not led.get("validated") or "transactions" not in lg or int(lg.get("ledger_index", -1)) != li:
        # incomplete answer (seen on the public RPC): treat like a transient error
        raise RpcError(f"ledger {li}: incomplete answer validated={led.get('validated')}")
    now = rpc.call("ledger_entry", {"index": NUNL_INDEX, "ledger_index": li}, absent_ok=True)
    nxt = rpc.call("ledger_entry", {"index": NUNL_INDEX, "ledger_index": li + 1}, absent_ok=True)
    return {
        "ev": "flag",
        "li": li,
        "lh": lg.get("ledger_hash"),
        "ct": lg.get("close_time"),
        "ntx": len(lg["transactions"]),
        "mods": unl_mods(lg["transactions"]),
        "nunl": nunl_view(now and now.get("node")),
        "next": nunl_view(nxt and nxt.get("node")),
    }


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


def load_done(path):
    done, bad = set(), 0
    if os.path.exists(path):
        with open(path) as fh:
            for line in fh:
                try:
                    e = json.loads(line)
                    if e["ev"] == "flag":
                        done.add(e["li"])
                except (ValueError, KeyError, TypeError):
                    bad += 1
    return done, bad


def parse_range(s):
    """complete_ledgers '7500054-7555763' (possibly 'a-b,c-d') -> the LAST contiguous span."""
    lo, _, hi = s.split(",")[-1].partition("-")
    return int(lo), int(hi or lo)


def flags_in(lo, hi):
    """Flag ledgers F with F and F+1 both in [lo, hi]."""
    first = -(-lo // FLAG) * FLAG
    return list(range(first, hi, FLAG))


def run(args):
    global _stop
    os.makedirs(args.out, exist_ok=True)
    lf = open(args.log, "a") if args.log else None
    path = os.path.join(args.out, "nunl.jsonl")
    torn = drop_torn_tail(path)
    done, bad = load_done(path)
    rpc = Rpc(args.rpc, gap=args.gap, retries=args.retries, logf=lf)
    cl = rpc.call("server_info", {})["info"]["complete_ledgers"]
    lo, hi = parse_range(cl)
    lo = max(lo, args.from_ledger or lo)
    hi = min(hi, args.to_ledger or hi)
    todo = [f for f in flags_in(lo, hi) if f not in done]
    fh = open(path, "a")

    def emit(e):
        fh.write(json.dumps(e, separators=(",", ":")) + "\n")
        fh.flush()
        os.fsync(fh.fileno())

    emit(
        {
            "ev": "session",
            "start": round(time.time(), 3),
            "rpc": args.rpc,
            "complete_ledgers": cl,
            "from": lo,
            "to": hi,
            "have": len(done),
            "torn_bytes": torn,
            "bad_lines": bad,
        }
    )
    log(
        f"start rpc={args.rpc} complete_ledgers={cl} range={lo}-{hi} flags_in_range={len(flags_in(lo, hi))}"
        f" already={len(done)} todo={len(todo)} torn_bytes={torn} bad_lines={bad} pid={os.getpid()}",
        lf,
    )
    signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__("_stop", "SIGTERM"))
    signal.signal(signal.SIGINT, lambda *_: globals().__setitem__("_stop", "SIGINT"))
    n = errors = 0
    for li in todo:
        if _stop:
            break
        try:
            e = fetch_flag(rpc, li)
        except RpcError as ex:
            errors += 1
            emit({"ev": "error", "li": li, "error": str(ex)})
            log(f"ERROR flag {li}: {ex}", lf)
            continue
        emit(e)
        n += 1
        if e["mods"] or e["nunl"]:
            log(f"flag {li}: mods={len(e['mods'])} nunl={e['nunl']}", lf)
        if n % 25 == 0:
            log(f"progress {n}/{len(todo)} last={li} calls={rpc.calls} retried={rpc.retried}", lf)
    reason = _stop or "range complete"
    emit(
        {
            "ev": "stop",
            "t": round(time.time(), 3),
            "reason": reason,
            "done": n,
            "errors": errors,
            "calls": rpc.calls,
            "retried": rpc.retried,
        }
    )
    log(
        f"stop ({reason}) collected={n} errors={errors} total_in_file={len(done) + n}"
        f" calls={rpc.calls} retried={rpc.retried}",
        lf,
    )
    return 1 if errors else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--rpc", default="https://rpc.testnet.postfiat.org")
    ap.add_argument("--out", required=True)
    ap.add_argument("--log")
    ap.add_argument("--from", dest="from_ledger", type=int, help="default: first ledger of complete_ledgers")
    ap.add_argument("--to", dest="to_ledger", type=int, help="default: last ledger of complete_ledgers")
    ap.add_argument("--gap", type=float, default=0.2, help="seconds between requests")
    ap.add_argument("--retries", type=int, default=6)
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
