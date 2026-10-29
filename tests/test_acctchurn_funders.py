import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "acctchurn"))
import funders_churn as funders  # noqa: E402
from collect_rewardflow import Rpc  # noqa: E402  (path added by funders)


def entry(ledger, account, created=None, balance="0", seq=None):
    nodes = [{"ModifiedNode": {"LedgerEntryType": "AccountRoot"}}]
    if created:
        nodes.append(
            {
                "CreatedNode": {
                    "LedgerEntryType": "AccountRoot",
                    "NewFields": {
                        "Account": created,
                        "Balance": balance,
                        "Sequence": seq if seq is not None else ledger,
                    },
                }
            }
        )
    return {
        "tx": {
            "Account": account,
            "TransactionType": "Payment",
            "ledger_index": ledger,
            "hash": f"H{ledger}{created or ''}",
        },
        "meta": {"AffectedNodes": nodes},
        "validated": True,
    }


class FakeServer:
    """Pages account_tx 2 entries at a time and, like the real public RPC, ignores the ledger window."""

    def __init__(self, history, fail_first=0):
        self.history, self.fail_first, self.seen = history, fail_first, 0

    def __call__(self, body):
        self.seen += 1
        if self.seen <= self.fail_first:
            return {"result": {"status": "error", "error": "slowDown"}}
        p = json.loads(body)["params"][0]
        txs = self.history.get(p["account"], [])
        start = p.get("marker", 0)
        page = txs[start : start + 2]
        res = {"status": "success", "transactions": page}
        if start + 2 < len(txs):
            res["marker"] = start + 2
        return {"result": res}


def rpc(server):
    return Rpc("https://fake", gap=0, post=server, sleep=lambda s: None)


DIFF = {
    "a": {"ledger_index": 100},
    "b": {"ledger_index": 200},
    "accounts": {
        "created": [
            {"address": "rB", "sequence": 150},  # funded by rF, never sent a tx
            {"address": "rC", "sequence": 165},  # funded by rB (chain rF -> rB -> rC), active
            {"address": "rD", "sequence": 170},  # funded by rF
        ]
    },
}

HISTORY = {
    # older txs before the window come first (the server returns them although lo=101)
    "rB": [entry(50, "rX"), entry(150, "rF", created="rB", balance="20000000"), entry(160, "rB", "rC")],
    "rC": [entry(160, "rB", created="rC", balance="10000000")],
    "rD": [entry(170, "rF", created="rD", balance="15000000")],
}


def test_graph():
    r = funders.build(DIFF, rpc(FakeServer(HISTORY)), reserve_base=10_000_000)
    assert r["not_found"] == []
    by = {c["address"]: c for c in r["created"]}
    assert by["rB"]["funder"] == "rF" and by["rB"]["reserve_multiple"] == 2.0 and by["rB"]["active"] is False
    assert by["rC"]["funder"] == "rB" and by["rC"]["active"] is True
    assert r["never_active"] == 2  # rB (seq 150 = created) and rD
    assert r["funders"][0] == {"funder": "rF", "accounts": 2, "drops": 35_000_000, "share": 0.6667}
    assert r["chains"] == [{"address": "rC", "depth": 2, "root_funder": "rF"}]


def test_creation_before_window_is_not_found():
    hist = {"rB": [entry(90, "rF", created="rB", balance="1")]}  # created before A+1
    doc = dict(DIFF, accounts={"created": [{"address": "rB", "sequence": 90}]})
    r = funders.build(doc, rpc(FakeServer(hist)), reserve_base=10_000_000)
    assert r["not_found"] == ["rB"] and r["created"] == []
    assert funders.find_creation(rpc(FakeServer(hist)), "rB", 101, 200) is None


def test_creation_after_window_stops_paging():
    srv = FakeServer({"rB": [entry(201, "rF", created="rB")] + [entry(300 + i, "rB") for i in range(10)]})
    assert funders.find_creation(rpc(srv), "rB", 101, 200) is None
    assert srv.seen == 1  # returned on the first out-of-window ledger, no further pages


def test_retries_are_counted():
    r = funders.build(DIFF, rpc(FakeServer(HISTORY, fail_first=2)), reserve_base=10_000_000)
    assert r["rpc"]["retries"] == 2 and r["not_found"] == []


def test_report_end_to_end(tmp_path):
    import diff_churn
    import report_churn
    from test_acctchurn_diff import acct, write

    full = [
        "rFunderAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "rNewBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
        "rGoneCCCCCCCCCCCCCCCCCCCCCCCCCCC",
    ]
    pa = write(tmp_path / "a.jsonl.gz", 100, 1500, [acct(1, full[0], 1000), acct(3, full[2], 500)])
    pb = write(tmp_path / "b.jsonl.gz", 200, 1488, [acct(1, full[0], 1448), acct(2, full[1], 40, seq=150)])
    ha, a = diff_churn.load(pa)
    hb, b = diff_churn.load(pb)
    d = diff_churn.diff(ha, a, hb, b, fees_sum=12)
    assert d["ok"]
    hist = {full[1]: [entry(150, full[0], created=full[1], balance="40")]}
    f = funders.build(json.loads(json.dumps(d)), rpc(FakeServer(hist)), reserve_base=10)
    text = report_churn.render(d, f)
    assert "| burned_eq_fee_sum | PASS |" in text and "Never active in the window" in text
    assert "rFunderA…" in text and "rGoneCCC…" in text
    for addr in full:
        assert addr not in text  # addresses are only ever printed truncated
    assert report_churn.render(d, f) == text
