import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "acctchurn"))
import crosscheck_churn as crosscheck  # noqa: E402
from funders_churn import churn_rpc  # noqa: E402
from test_acctchurn_diff import acct, write  # noqa: E402

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


FLAKY = {"left": 0, "error": "lgrNotFound"}


def server(body):
    req = json.loads(body)
    if FLAKY["left"] > 0:
        FLAKY["left"] -= 1
        return {"result": {"status": "error", "error": FLAKY["error"]}}
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
    "accounts": {
        "created": [{"address": "r4", "balance_drops": 40}],
        "changed": [{"address": "r1", "balance_a_drops": 1000, "owner_count_a": 0}],
        "deleted": [{"address": "r3", "balance_drops": 200, "owner_count": 0}],
    },
}
FUND = {"created": [{"address": "r4", "tx_hash": "HC4", "ledger": 150, "funder": "r1", "funded_drops": 40}]}


def go(tmp_path, negative=False, fund=FUND):
    # r9 is unchanged between A and B, so it is never in the sample pool
    snap = write(
        tmp_path / "b.jsonl.gz", 200, 1237, [acct(1, "r1", 1190), acct(4, "r4", 40), acct(9, "r9", 7)]
    )
    lines = []
    rc = crosscheck.run(
        DIFF,
        fund,
        snap,
        churn_rpc("https://fake", gap=0, post=server, sleep=lambda s: None),
        count=5,
        seed=1,
        negative=negative,
        out=lines.append,
    )
    return rc, lines


def test_all_ok(tmp_path):
    rc, lines = go(tmp_path)
    assert rc == 0
    # r1 @B/@A, r4 @B/@A (actNotFound), r4 creating tx, deleted r3 @A/@B
    assert sum(x.startswith("OK") for x in lines) == 7
    assert not any("r9" in x for x in lines)
    assert any(
        x.startswith("OK sample r4") and "@A (created) expected=actNotFound got=None" in x for x in lines
    )
    assert lines[-1].startswith("SUMMARY checked=7 mismatches=0 sample=2 of pool=2")
    assert lines[-1].endswith("retries=0 retries_by_error={}")


def test_negative_control_mismatches_every_balance(tmp_path):
    rc, lines = go(tmp_path, negative=True)
    assert rc == 1
    bad = [x for x in lines if x.startswith("MISMATCH")]
    # r1 @B and @A, r4 @B, creating tx, deleted @A; the actNotFound checks carry no balance
    assert len(bad) == 5


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


def test_lgrnotfound_is_retried_and_counted_not_a_mismatch(tmp_path):
    for err in ("lgrNotFound", "internal"):
        FLAKY.update(left=2, error=err)
        rc, lines = go(tmp_path)
        assert rc == 0 and not any(x.startswith("MISMATCH") for x in lines)
        assert lines[-1].endswith(f'retries=2 retries_by_error={{"{err}": 2}}')


def test_persistent_lgrnotfound_is_an_rpc_failure_not_a_mismatch(tmp_path):
    import pytest

    FLAKY.update(left=100, error="lgrNotFound")
    try:
        with pytest.raises(crosscheck.RpcError, match="giving up after 6 attempts: lgrNotFound"):
            go(tmp_path)
    finally:
        FLAKY["left"] = 0
