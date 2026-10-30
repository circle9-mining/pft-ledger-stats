import gzip
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "keyposture"))
from collect_key import LSF_DISABLE_MASTER, key_lines, key_reasons  # noqa: E402
from count_key import compare, snapshot_indexes  # noqa: E402


def mod(prev, fin, let="AccountRoot"):
    return {"ModifiedNode": {"LedgerEntryType": let, "PreviousFields": prev, "FinalFields": fin}}


def test_types_always_relevant():
    for tt in ("SetRegularKey", "SignerListSet", "AccountSet"):
        assert key_reasons({"TransactionType": tt}, {"AffectedNodes": []}) == ["type"]


def test_plain_payment_fee_only_not_relevant():
    meta = {
        "AffectedNodes": [mod({"Balance": "10", "Sequence": 1}, {"Balance": "9", "Sequence": 2, "Flags": 0})]
    }
    assert key_reasons({"TransactionType": "Payment"}, meta) == []


def test_account_created_and_deleted():
    c = {"AffectedNodes": [{"CreatedNode": {"LedgerEntryType": "AccountRoot", "NewFields": {}}}]}
    d = {"AffectedNodes": [{"DeletedNode": {"LedgerEntryType": "AccountRoot", "FinalFields": {}}}]}
    assert key_reasons({"TransactionType": "Payment"}, c) == ["account_created"]
    assert key_reasons({"TransactionType": "AccountDelete"}, d) == ["account_deleted"]


def test_signerlist_node_any_kind():
    for kind in ("CreatedNode", "ModifiedNode", "DeletedNode"):
        meta = {"AffectedNodes": [{kind: {"LedgerEntryType": "SignerList"}}]}
        assert key_reasons({"TransactionType": "AccountDelete"}, meta) == ["signerlist"]


def test_regularkey_and_disablemaster_bit():
    rk = {"AffectedNodes": [mod({"RegularKey": "rX"}, {"Flags": 0})]}
    assert key_reasons({"TransactionType": "Batch"}, rk) == ["regularkey"]
    flip = {"AffectedNodes": [mod({"Flags": 0}, {"Flags": LSF_DISABLE_MASTER})]}
    assert key_reasons({"TransactionType": "Batch"}, flip) == ["disablemaster"]
    other = {"AffectedNodes": [mod({"Flags": 0}, {"Flags": 0x00800000})]}
    assert key_reasons({"TransactionType": "Batch"}, other) == []


def test_key_lines_sorted_and_counted():
    led = {
        "ledger_index": "10",
        "close_time": 800000000,
        "transactions": [
            {"TransactionType": "Payment", "metaData": {"TransactionIndex": 0, "AffectedNodes": []}},
            {"TransactionType": "SetRegularKey", "metaData": {"TransactionIndex": 3, "AffectedNodes": []}},
            {"TransactionType": "AccountSet", "meta": {"TransactionIndex": 1, "AffectedNodes": []}},
        ],
    }
    lines, n = key_lines(led)
    assert n == 3
    assert [x["tx_index"] for x in lines] == [1, 3]
    assert all("metaData" not in x["tx"] and "meta" not in x["tx"] for x in lines)


def _snap(tmp_path, objs, paged=None):
    p = tmp_path / "s.jsonl.gz"
    hdr = {"ledger_index": 5, "ledger_hash": "H", "objects_paged": len(objs) if paged is None else paged}
    with gzip.open(p, "wt") as fh:
        fh.write(json.dumps(hdr) + "\n")
        for o in objs:
            fh.write(json.dumps(o) + "\n")
    return str(p)


OBJS = [
    {"LedgerEntryType": "AccountRoot", "index": "A1"},
    {"LedgerEntryType": "AccountRoot", "index": "A2"},
    {"LedgerEntryType": "SignerList", "index": "S1"},
    {"LedgerEntryType": "Offer", "index": "O1"},
]


def test_count_compare_equal(tmp_path):
    _, sets = snapshot_indexes(_snap(tmp_path, OBJS))
    res, ok = compare(sets, {"account": ["A2", "A1"], "signer_list": ["S1"]})
    assert ok and res["AccountRoot"]["snapshot"] == 2


def test_count_compare_missing_extra_dup(tmp_path):
    _, sets = snapshot_indexes(_snap(tmp_path, OBJS))
    res, ok = compare(sets, {"account": ["A1", "A3", "A3"], "signer_list": ["S1"]})
    assert not ok
    r = res["AccountRoot"]
    assert r["only_in_snapshot"] == ["A2"] and r["only_in_paging"] == ["A3"] and r["paged_duplicates"] == 1
    assert res["SignerList"]["ok"]


def test_snapshot_object_count_mismatch(tmp_path):
    import pytest

    with pytest.raises(ValueError, match="header says"):
        snapshot_indexes(_snap(tmp_path, OBJS, paged=5))
