#!/usr/bin/env python3
"""Cross-check random validated tx hashes from flow.jsonl against a public Post Fiat RPC `tx` call.

For each of --count random hashes (seeded) recorded as validated, asks the public node
(read-only `tx`, >= 1 s between requests) and compares: validated == true, same ledger_index,
same TransactionResult. Prints one line per hash and a summary. Exit 0 = all match, 1 = any mismatch.

Usage: crosscheck_flow.py data/txflow/flow.jsonl [--count 20] [--seed N] [--rpc URL]
"""

import argparse
import json
import random
import sys
import time
import urllib.request


def rpc_tx(url, h, timeout=20):
    body = json.dumps({"method": "tx", "params": [{"transaction": h, "binary": False}]}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)["result"]


def main(argv=None, fetch=rpc_tx, sleep=time.sleep):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("flow")
    ap.add_argument("--count", type=int, default=20)
    ap.add_argument("--seed", type=int, default=20261025)
    ap.add_argument("--rpc", default="https://rpc.testnet.postfiat.org")
    a = ap.parse_args(argv)
    val = {}
    with open(a.flow) as fh:
        for line in fh:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("ev") == "validated":
                val.setdefault(e["hash"], e)
    hashes = sorted(val)
    pick = random.Random(a.seed).sample(hashes, min(a.count, len(hashes)))
    print(f"crosscheck {len(pick)} of {len(hashes)} validated hashes (seed {a.seed}) against {a.rpc} tx")
    ok = 0
    for i, h in enumerate(pick):
        if i:
            sleep(1.0)
        mine = val[h]
        try:
            r = fetch(a.rpc, h)
            got = (r.get("validated"), r.get("ledger_index"), (r.get("meta") or {}).get("TransactionResult"))
            err = r.get("error")
        except Exception as ex:  # network errors count as mismatch, never skipped silently
            got, err = (None, None, None), f"{type(ex).__name__}: {ex}"
        match = got == (True, mine["ledger_index"], mine.get("result"))
        ok += match
        print(
            f"{'MATCH' if match else 'MISMATCH'} {h}"
            f" collected ledger={mine['ledger_index']} result={mine.get('result')}"
            f" | public validated={got[0]} ledger={got[1]} result={got[2]}{' error=' + err if err else ''}"
        )
    print(f"RESULT: {ok}/{len(pick)} match")
    return 0 if pick and ok == len(pick) else 1


if __name__ == "__main__":
    sys.exit(main())
