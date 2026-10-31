#!/usr/bin/env python3
"""Independent account-count completeness check for a state snapshot (Python 3 stdlib only).

Pages the SAME pinned ledger a second time with `ledger_data` filtered by type (`account`,
then `signer_list`, binary=true so only the object index is used), records the marker chain,
and compares the index sets with the AccountRoot / SignerList objects of the snapshot file.

Run it right after state/snapshot.py: a node with online_delete keeps a ledger only for a
few hundred ledgers. Writes --json with counts, marker chains and set differences.

Exit codes: 0 = both index sets equal the snapshot's; 1 = a difference (listed);
2 = paging failed (marker chain broken, ledger hash changed, ledger gone) or bad snapshot.

--offline COUNT_JSON makes no RPC call: it re-reads the published snapshot and checks it against a
recorded --json result (same ledger index and hash, continuous marker chains whose page sizes sum
to the recorded paged count, no recorded differences, and snapshot count = recorded paged count).
The recorded file holds counts and marker chains, not the paged index lists, so offline mode
reproduces the counts; the set equality itself was checked at collection time. Same exit codes
(2 = bad snapshot, bad count file or broken recorded chain).
"""

import argparse
import gzip
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "state"))
from snapshot import Rpc  # noqa: E402

TYPES = {"account": "AccountRoot", "signer_list": "SignerList"}


def snapshot_indexes(path):
    """(header, {LedgerEntryType: set(index)}) for AccountRoot and SignerList of a snapshot."""
    want = set(TYPES.values())
    out = {t: set() for t in want}
    with gzip.open(path, "rt") as fh:
        header = json.loads(fh.readline())
        n = 0
        for line in fh:
            o = json.loads(line)
            n += 1
            if o.get("LedgerEntryType") in want:
                if o["index"] in out[o["LedgerEntryType"]]:
                    raise ValueError(f"duplicate {o['LedgerEntryType']} {o['index']} in snapshot")
                out[o["LedgerEntryType"]].add(o["index"])
    if n != header.get("objects_paged"):
        raise ValueError(f"snapshot has {n} objects, header says {header.get('objects_paged')}")
    return header, out


def page_type(rpc, ledger_index, ledger_hash, typ, limit):
    """Page one ledger_data type filter. Returns (index list, marker chain)."""
    idx, chain, marker = [], [], None
    while True:
        params = {"ledger_index": ledger_index, "binary": True, "type": typ, "limit": limit}
        if marker:
            params["marker"] = marker
        r = rpc.call("ledger_data", params)
        if r["ledger_hash"] != ledger_hash:
            raise RuntimeError(f"ledger hash {r['ledger_hash']} != snapshot {ledger_hash}")
        idx.extend(o["index"] for o in r["state"])
        chain.append({"marker_in": marker, "marker_out": r.get("marker"), "n": len(r["state"])})
        marker = r.get("marker")
        if not marker:
            return idx, chain


def compare(snap_sets, paged):
    """Per type: counts and differences. Returns (result dict, ok)."""
    res, ok = {}, True
    for typ, let in TYPES.items():
        s, p = snap_sets[let], paged[typ]
        dup = len(p) - len(set(p))
        only_s, only_p = sorted(s - set(p)), sorted(set(p) - s)
        good = not (dup or only_s or only_p)
        ok &= good
        res[let] = {
            "snapshot": len(s),
            "paged": len(p),
            "paged_duplicates": dup,
            "only_in_snapshot": only_s,
            "only_in_paging": only_p,
            "ok": good,
        }
    return res, ok


def check_chain(chain):
    """Recorded marker chain is continuous: starts at None, each marker_in = previous marker_out,
    ends at None. Returns the summed page sizes. Raises ValueError otherwise."""
    if not chain:
        raise ValueError("empty marker chain")
    prev = None
    for i, page in enumerate(chain):
        if page["marker_in"] != prev:
            raise ValueError(f"marker chain broken at page {i}")
        prev = page["marker_out"]
    if prev is not None:
        raise ValueError("marker chain does not end")
    return sum(page["n"] for page in chain)


def offline(header, sets, rec):
    """Compare snapshot index sets with a recorded count_key --json result. Returns (lines, ok);
    raises ValueError/KeyError/TypeError on a bad count file."""
    if (rec["ledger_index"], rec["ledger_hash"]) != (header["ledger_index"], header["ledger_hash"]):
        raise ValueError(
            f"count file is for ledger {rec['ledger_index']} {rec['ledger_hash']}, snapshot is "
            f"{header['ledger_index']} {header['ledger_hash']}"
        )
    lines, ok = [], True
    for typ, let in TYPES.items():
        r = rec["result"][let]
        chained = check_chain(rec["marker_chains"][typ])
        if chained != r["paged"]:
            raise ValueError(f"{typ} marker chain pages sum to {chained}, recorded paged {r['paged']}")
        n = len(sets[let])
        diffs = r["paged_duplicates"] + len(r["only_in_snapshot"]) + len(r["only_in_paging"])
        good = n == r["snapshot"] == r["paged"] and diffs == 0 and r["ok"] is True
        ok &= good
        lines.append(
            f"{let}: snapshot file {n} recorded paged {r['paged']} (pages {len(rec['marker_chains'][typ])}, "
            f"chain continuous) recorded differences {diffs} -> {'OK' if good else 'MISMATCH'}"
        )
    return lines, ok


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("snapshot", help="snapshot-<ledger>.jsonl.gz from state/snapshot.py")
    p.add_argument("--json", help="write the result here (required unless --offline)")
    p.add_argument("--offline", metavar="COUNT_JSON", help="no RPC: check against a recorded --json result")
    p.add_argument("--limit", type=int, default=1024)
    p.add_argument("--container", default="postfiatd")
    p.add_argument("--port", type=int, default=5005)
    p.add_argument("--no-sudo", action="store_true")
    a = p.parse_args(argv)
    try:
        header, sets = snapshot_indexes(a.snapshot)
    except (OSError, ValueError, KeyError) as e:
        print(f"ERROR bad snapshot: {e}")
        return 2
    if a.offline:
        try:
            with open(a.offline) as fh:
                lines, ok = offline(header, sets, json.load(fh))
        except (OSError, ValueError, KeyError, TypeError) as e:
            print(f"ERROR bad count file: {e}")
            return 2
        print("\n".join(lines))
        print(f"RESULT {'PASS' if ok else 'FAIL'} ledger {header['ledger_index']} offline")
        return 0 if ok else 1
    if not a.json:
        p.error("--json is required unless --offline")
    rpc = Rpc(a.container, a.port, 2.0, ["docker"] if a.no_sudo else ["sudo", "docker"])
    paged, chains = {}, {}
    try:
        for typ in TYPES:
            paged[typ], chains[typ] = page_type(
                rpc, header["ledger_index"], header["ledger_hash"], typ, a.limit
            )
    except (RuntimeError, LookupError) as e:
        print(f"ERROR paging ledger {header['ledger_index']}: {e}")
        return 2
    res, ok = compare(sets, paged)
    out = {
        "ledger_index": header["ledger_index"],
        "ledger_hash": header["ledger_hash"],
        "result": res,
        "marker_chains": chains,
        "ok": ok,
    }
    with open(a.json, "w") as fh:
        json.dump(out, fh, indent=1, sort_keys=True)
        fh.write("\n")
    for typ, let in TYPES.items():
        r = res[let]
        print(
            f"{let}: snapshot {r['snapshot']} paged {r['paged']} (pages {len(chains[typ])}) "
            f"dup {r['paged_duplicates']} only_snapshot {len(r['only_in_snapshot'])} "
            f"only_paging {len(r['only_in_paging'])} -> {'OK' if r['ok'] else 'MISMATCH'}"
        )
    print(f"RESULT {'PASS' if ok else 'FAIL'} ledger {header['ledger_index']}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
