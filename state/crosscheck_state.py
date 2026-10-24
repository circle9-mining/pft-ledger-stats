#!/usr/bin/env python3
"""Cross-check a snapshot against `account_info ledger_index=<same ledger>` on the node.

Picks --count random AccountRoots from the snapshot (seeded), asks the node for each
account's Balance and OwnerCount at the snapshot's own ledger, and compares. An account
answered with lgrNotFound / actNotFound is skipped and replaced by another random account
(lgrNotFound for every try means the node has deleted the ledger: exit 2).
Also re-checks that the snapshot's type counts sum to objects_paged.

--negative is the control: it adds 1 drop to every expected Balance before comparing,
so a working check must report every account as a MISMATCH and exit 1.

Accounts are printed by their AccountRoot object index (ledger object ID), never by address.

Exit 0 all match, 1 any mismatch, 2 not enough accounts could be checked.

Usage:
  ./crosscheck_state.py data/state/snapshot-N.jsonl.gz --count 10 --seed 1 [--negative]
"""

import argparse  # noqa: I001  (analyze/snapshot are sibling scripts in state/)
import random
import sys
from collections import Counter

from analyze import load
from snapshot import Rpc


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("snapshot")
    p.add_argument("--count", type=int, default=10)
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--negative", action="store_true", help="control: expected Balance + 1 drop")
    p.add_argument("--container", default="postfiatd")
    p.add_argument("--port", type=int, default=5005)
    p.add_argument("--no-sudo", action="store_true")
    args = p.parse_args(argv)

    header, objs = load(args.snapshot)
    idx = header["ledger_index"]
    types = Counter(o["LedgerEntryType"] for o in objs)
    ok_count = sum(types.values()) == header["objects_paged"]
    print(f"snapshot ledger {idx} {header['ledger_hash']}: sum of type counts {sum(types.values())} vs "
          f"objects_paged {header['objects_paged']}: {'OK' if ok_count else 'MISMATCH'}")
    accts = [o for o in objs if o["LedgerEntryType"] == "AccountRoot"]
    rng = random.Random(args.seed)
    pool = rng.sample(accts, len(accts))
    rpc = Rpc(args.container, args.port, 2.0, ["docker"] if args.no_sudo else ["sudo", "docker"])
    mode = "NEGATIVE CONTROL (expected Balance + 1 drop)" if args.negative else "positive"
    print(f"mode: {mode}; seed {args.seed}; account_info ledger_index={idx}")
    print(f"{'#':>2} {'AccountRoot index':<64} {'snap Balance':>16} {'node Balance':>16} "
          "snapOC nodeOC result")
    checked = bad = skipped = 0
    for o in pool:
        if checked >= args.count:
            break
        try:
            r = rpc.call("account_info", {"account": o["Account"], "ledger_index": idx})
        except LookupError as e:
            skipped += 1
            print(f"   {o['index']:<64} skip+replace ({e})")
            if str(e) == "lgrNotFound" and skipped >= 3 and checked == 0:
                print("node no longer holds this ledger (lgrNotFound x3): exit 2")
                return 2
            continue
        ad = r["account_data"]
        want_bal = int(o["Balance"]) + (1 if args.negative else 0)
        got_bal = int(ad["Balance"])
        want_oc, got_oc = int(o.get("OwnerCount", 0)), int(ad.get("OwnerCount", 0))
        if int(r["ledger_index"]) != idx:
            raise RuntimeError(f"node answered for ledger {r['ledger_index']} not {idx}")
        res = "MATCH" if (want_bal, want_oc) == (got_bal, got_oc) else "MISMATCH"
        checked += 1
        bad += res != "MATCH"
        print(f"{checked:>2} {o['index']:<64} {want_bal:>16} {got_bal:>16} {want_oc:>6} {got_oc:>6} {res}")
    print(f"checked {checked}, mismatches {bad}, skipped+replaced {skipped}")
    if checked < args.count:
        return 2
    return 0 if bad == 0 and ok_count else 1


if __name__ == "__main__":
    sys.exit(main())
