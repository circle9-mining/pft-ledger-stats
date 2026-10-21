#!/usr/bin/env python3
"""Cross-check random collected ledgers against a second node (e.g. your sentry).

Picks --count random ledgers from data/ledgers.csv that both nodes still hold
(light nodes keep a short history), fetches each from both nodes with read-only
`ledger` RPC, and compares ledger_hash and close_time. Writes a CSV that
analyze.py can embed. The remote node is reached via `ssh <alias> docker exec`;
only the generic labels "local"/"second" are written to output.
"""

import argparse
import csv
import json
import random
import shlex
import subprocess
import sys
import time

from pftstats import stats


def rpc(prefix, container, method, params, remote):
    body = json.dumps({"method": method, "params": [params]})
    cmd = ["docker", "exec", container, "curl", "-s", "-m", "10", "-X", "POST",
           "-H", "Content-Type: application/json", "-d", body, "http://127.0.0.1:5005/"]
    full = prefix + ([shlex.join(cmd)] if remote else cmd)
    out = subprocess.run(full, capture_output=True, text=True, timeout=60, check=True)
    time.sleep(1.0)  # <= 1 request/second per node
    return json.loads(out.stdout)["result"]


def parse_ranges(s):
    """'1-5,8-9' -> [(1, 5), (8, 9)]; 'empty' -> []."""
    out = []
    for part in (s or "").split(","):
        if "-" in part:
            a, b = part.split("-")
            out.append((int(a), int(b)))
        elif part.strip().isdigit():
            out.append((int(part), int(part)))
    return out


def held(ranges, i):
    return any(a <= i <= b for a, b in ranges)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data", default="data")
    p.add_argument("--ssh", required=True, help="ssh alias of the second node host")
    p.add_argument("--remote-container", default="postfiatd")
    p.add_argument("--local-container", default="postfiatd")
    p.add_argument("--count", type=int, default=5)
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--out", default="data/crosscheck.csv")
    a = p.parse_args(argv)

    local = (["sudo"], a.local_container, False)
    second = (["ssh", "-o", "BatchMode=yes", a.ssh], a.remote_container, True)
    rows = {r["ledger_index"]: r for r in stats.load_ledgers(f"{a.data}/ledgers.csv")}
    lr = parse_ranges(rpc(*local[:2], "server_info", {}, local[2])["info"]["complete_ledgers"])
    sr = parse_ranges(rpc(*second[:2], "server_info", {}, second[2])["info"]["complete_ledgers"])
    candidates = sorted(i for i in rows if held(lr, i) and held(sr, i))
    print(f"collected {len(rows)} ledgers; held by both nodes now: {len(candidates)}"
          + (f" ({candidates[0]}-{candidates[-1]})" if candidates else ""))
    if len(candidates) < a.count:
        print("not enough overlapping ledgers", file=sys.stderr)
        return 2
    seed = a.seed if a.seed is not None else int(time.time())
    pick = sorted(random.Random(seed).sample(candidates, a.count))
    print(f"seed {seed}; picked {pick}")

    results, ok = [], True
    for i in pick:
        q = {"ledger_index": i}
        lo = rpc(*local[:2], "ledger", q, local[2])["ledger"]
        se = rpc(*second[:2], "ledger", q, second[2])["ledger"]
        r = {
            "ledger_index": i,
            "csv_hash": rows[i]["ledger_hash"],
            "local_hash": lo["ledger_hash"],
            "second_hash": se["ledger_hash"],
            "csv_close_time": rows[i]["close_time"],
            "local_close_time": int(lo["close_time"]),
            "second_close_time": int(se["close_time"]),
        }
        r["hash_match"] = "yes" if r["csv_hash"] == r["local_hash"] == r["second_hash"] else "NO"
        same_ct = r["csv_close_time"] == r["local_close_time"] == r["second_close_time"]
        r["close_time_match"] = "yes" if same_ct else "NO"
        ok = ok and r["hash_match"] == r["close_time_match"] == "yes"
        print(f"ledger {i} hash csv={r['csv_hash'][:16]} local={r['local_hash'][:16]} "
              f"second={r['second_hash'][:16]} match={r['hash_match']} | close_time "
              f"{r['csv_close_time']}/{r['local_close_time']}/{r['second_close_time']} "
              f"match={r['close_time_match']}")
        results.append(r)
    with open(a.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(results[0]))
        w.writeheader()
        w.writerows(results)
    print(f"RESULT {sum(r['hash_match'] == 'yes' for r in results)}/{len(results)} hash matches, "
          f"{sum(r['close_time_match'] == 'yes' for r in results)}/{len(results)} close_time matches")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
