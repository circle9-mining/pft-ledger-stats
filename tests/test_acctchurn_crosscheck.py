import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "acctchurn"))
from funders import Rpc  # noqa: E402
from test_acctchurn_diff import acct, write  # noqa: E402

import crosscheck  # noqa: E402

# ledger -> address -> (Balance, OwnerCount); absent = actNotFound
STATE = {100: {"r1": (1000, 0), "r3": (200, 0)}, 200: {"r1": (1190, 0), "r4": (40, 0)}}
TX = {
    "HC4": {
        "validated": True,
        "ledger_index": 150,
        "Account": "r1",
        "meta": {
            "AffectedNodes": [
                {
                    "CreatedNode": {
                        "LedgerEntryType": "AccountRoot",
                        "NewFields": {"Account": "r4", "Balance": "40"},
                    }
                }
            ]
        },
    }
}


def server(body):
    req = json.loads(body)
    p = req["params"][0]
    if req["method"] == "tx":
        return {"result": dict(TX[p["transaction"]], status="success")}
    got = STATE[p["ledger_index"]].get(p["account"])
    if got is None:
        return {"result": {"status": "error", "error": "actNotFound"}}
    return {"result": {"status": "success", "account_data": {"Balance": str(got[0]), "OwnerCount": got[1]}}}


DIFF = {
    "a": {"ledger_index": 100},
    "b": {"ledger_index": 200},
    "accounts": {"deleted": [{"address": "r3", "balance_drops": 200, "owner_count": 0}]},
}
FUND = {"created": [{"address": "r4", "tx_hash": "HC4", "ledger": 150, "funder": "r1", "funded_drops": 40}]}


def go(tmp_path, negative=False, fund=FUND):
    snap = write(tmp_path / "b.jsonl.gz", 200, 1230, [acct(1, "r1", 1190), acct(4, "r4", 40)])
    lines = []
    rc = crosscheck.run(
        DIFF,
        fund,
        snap,
        Rpc("https://fake", gap=0, post=server, sleep=lambda s: None),
        count=5,
        seed=1,
        negative=negative,
        out=lines.append,
    )
    return rc, lines


def test_all_ok(tmp_path):
    rc, lines = go(tmp_path)
    assert rc == 0
    assert sum(x.startswith("OK") for x in lines) == 5  # 2 samples + 1 creating tx + deleted @A and @B
    assert lines[-1].startswith("SUMMARY checked=5 mismatches=0")


def test_negative_control_mismatches_every_balance(tmp_path):
    rc, lines = go(tmp_path, negative=True)
    assert rc == 1
    bad = [x for x in lines if x.startswith("MISMATCH")]
    # samples (2), creating tx (1), deleted @A (1); the actNotFound @B check carries no balance
    assert len(bad) == 4


def test_wrong_funder_is_caught(tmp_path):
    fund = {"created": [dict(FUND["created"][0], funder="rZ")]}
    rc, lines = go(tmp_path, fund=fund)
    assert rc == 1 and any(x.startswith("MISMATCH creating-tx") for x in lines)


def test_deleted_account_still_present_is_caught(tmp_path):
    STATE[200]["r3"] = (5, 0)
    try:
        rc, lines = go(tmp_path)
    finally:
        del STATE[200]["r3"]
    assert rc == 1 and any("MISMATCH deleted r3" in x and "@B" in x for x in lines)
