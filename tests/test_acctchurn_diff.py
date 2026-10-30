import gzip
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "acctchurn"))
import diff_churn as diff  # noqa: E402


def idx(n):
    return f"{n:064X}"


def acct(n, addr, bal, seq=1):
    return {
        "index": idx(n),
        "LedgerEntryType": "AccountRoot",
        "Account": addr,
        "Balance": str(bal),
        "Sequence": seq,
        "OwnerCount": 0,
    }


def write(path, ledger, total, objs, paged=None):
    header = {
        "format": "pft-state-snapshot/1",
        "ledger_index": ledger,
        "ledger_hash": "AB" * 32,
        "close_time_iso": "2026-10-29T00:00:00Z",
        "total_coins": total,
        "objects_paged": len(objs) if paged is None else paged,
    }
    with gzip.open(path, "wt") as fh:
        fh.write(json.dumps(header, sort_keys=True) + "\n")
        for o in objs:
            fh.write(json.dumps(o, sort_keys=True) + "\n")
    return str(path)


@pytest.fixture
def pair(tmp_path):
    # A: r1 1000, r2 500, r3 200 (deleted in B), escrow 100, channel 300 of which 50 paid -> 250 held
    a_objs = [
        acct(1, "r1", 1000),
        acct(2, "r2", 500),
        acct(3, "r3", 200),
        {"index": idx(10), "LedgerEntryType": "Escrow", "Amount": "100"},
        {"index": idx(11), "LedgerEntryType": "PayChannel", "Amount": "300", "Balance": "50"},
        {"index": idx(12), "LedgerEntryType": "RippleState", "Balance": {"currency": "X", "value": "5"}},
    ]
    total_a = 1000 + 500 + 200 + 100 + 250
    # B: r3 deleted (190 to r1, 10 burned), r4 created with 40 from r2, r2 paid 2 in fees
    b_objs = [
        acct(1, "r1", 1190),
        acct(2, "r2", 458, seq=3),
        acct(4, "r4", 40),
        {"index": idx(10), "LedgerEntryType": "Escrow", "Amount": "100"},
        {"index": idx(11), "LedgerEntryType": "PayChannel", "Amount": "300", "Balance": "50"},
        {"index": idx(12), "LedgerEntryType": "RippleState", "Balance": {"currency": "X", "value": "5"}},
    ]
    total_b = total_a - 12
    return (
        write(tmp_path / "a.jsonl.gz", 100, total_a, a_objs),
        write(tmp_path / "b.jsonl.gz", 200, total_b, b_objs),
        tmp_path,
    )


def run(pa, pb, fees=None):
    ha, a = diff.load(pa)
    hb, b = diff.load(pb)
    return diff.diff(ha, a, hb, b, fees)


def test_counts_and_accounts(pair):
    r = run(pair[0], pair[1], fees=12)
    t = r["by_type"]["AccountRoot"]
    assert (t["in_a"], t["in_b"], t["created"], t["deleted"], t["changed"], t["unchanged"]) == (
        3,
        3,
        1,
        1,
        2,
        0,
    )
    assert r["by_type"]["Escrow"]["unchanged"] == 1
    assert [x["address"] for x in r["accounts"]["created"]] == ["r4"]
    assert [x["address"] for x in r["accounts"]["deleted"]] == ["r3"]
    assert r["native"]["a"] == {"account_root": 1700, "escrow": 100, "paychannel": 250, "total": 2050}
    assert r["native"]["burned_drops"] == 12
    assert r["ok"] and all(r["checks"].values())


def test_changed_rows_and_balance_aggregates(pair):
    r = run(pair[0], pair[1], fees=12)
    ch = {x["address"]: x for x in r["accounts"]["changed"]}
    assert sorted(ch) == ["r1", "r2"]
    assert (ch["r1"]["balance_a_drops"], ch["r1"]["balance_b_drops"], ch["r1"]["delta_drops"]) == (
        1000,
        1190,
        190,
    )
    assert (ch["r2"]["delta_drops"], ch["r2"]["sequence_a"], ch["r2"]["sequence_b"]) == (-42, 1, 3)
    # r1 +190, r4 created +40, r2 -42, r3 deleted -200
    assert r["balance_changes"] == {
        "net_drops": -12,
        "gross_increase_drops": 230,
        "gross_decrease_drops": 242,
        "accounts_up": 2,
        "accounts_down": 2,
        "accounts_same_balance": 0,
    }
    assert r["checks"]["net_balance_change_eq_delta_account_root"]


def test_owner_count_only_change_is_same_balance(pair, tmp_path):
    ha, a = diff.load(pair[0])
    objs = [dict(o, OwnerCount=1) if o.get("Account") == "r1" else o for o in a.values()]
    b = write(tmp_path / "oc.jsonl.gz", 101, ha["total_coins"], objs)
    r = run(pair[0], b, fees=0)
    assert [x["address"] for x in r["accounts"]["changed"]] == ["r1"]
    assert r["balance_changes"]["accounts_same_balance"] == 1 and r["balance_changes"]["net_drops"] == 0
    assert r["ok"]


def test_fee_sum_mismatch_fails(pair):
    r = run(pair[0], pair[1], fees=11)
    assert r["checks"]["burned_eq_fee_sum"] is False and not r["ok"]
    assert diff.main([pair[0], pair[1], "--fees-sum", "11", "--out", str(pair[2] / "o.json")]) == 1


def test_tampered_balance_breaks_conservation(pair, tmp_path):
    ha, a = diff.load(pair[1])
    objs = list(a.values())
    objs[0] = dict(objs[0], Balance=str(int(objs[0]["Balance"]) + 1))  # one drop added
    bad = write(tmp_path / "bad.jsonl.gz", 200, ha["total_coins"], objs)
    r = run(pair[0], bad, fees=12)
    assert not r["checks"]["native_b_eq_total_coins_b"]
    assert not r["checks"]["delta_native_eq_minus_burned"]
    assert diff.main([pair[0], bad, "--out", str(tmp_path / "o.json")]) == 1


def test_dropped_page_is_rejected(pair, tmp_path):
    _, b = diff.load(pair[1])
    short = write(tmp_path / "short.jsonl.gz", 200, 0, list(b.values())[:-2], paged=len(b))
    with pytest.raises(diff.SnapshotError, match="objects_paged"):
        diff.load(short)
    assert diff.main([pair[0], short]) == 2


def test_duplicate_index_is_rejected(tmp_path):
    p = write(tmp_path / "dup.jsonl.gz", 1, 2, [acct(1, "r1", 1), acct(1, "r1", 1)])
    with pytest.raises(diff.SnapshotError, match="duplicate"):
        diff.load(p)


def test_b_must_be_later(pair):
    with pytest.raises(diff.SnapshotError, match="later"):
        run(pair[1], pair[0])


def test_output_is_deterministic(pair, tmp_path):
    o1, o2 = tmp_path / "1.json", tmp_path / "2.json"
    assert diff.main([pair[0], pair[1], "--fees-sum", "12", "--out", str(o1)]) == 0
    assert diff.main([pair[0], pair[1], "--fees-sum", "12", "--out", str(o2)]) == 0
    assert o1.read_bytes() == o2.read_bytes()
