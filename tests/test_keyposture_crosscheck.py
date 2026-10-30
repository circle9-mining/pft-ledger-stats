import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "keyposture"))
sys.path.insert(0, os.path.dirname(__file__))
import crosscheck_key as X  # noqa: E402
import posture_key as P  # noqa: E402
import reconcile_key as R  # noqa: E402
from test_keyposture_reconcile import A0, A1, A2, A4, A5, LINES, SNAP_A, SNAP_B, write_snap, write_tx  # noqa: E402


def server(snaps, txs, fail_first=0, v2=False):
    """Fake public RPC answering from {ledger: [objects]}; first `fail_first` calls answer lgrNotFound."""
    calls = {"n": 0}

    def post(body):
        calls["n"] += 1
        if calls["n"] <= fail_first:
            return {"result": {"status": "error", "error": "lgrNotFound"}}
        req = json.loads(body)
        m, prm = req["method"], req["params"][0]
        if m == "account_info":
            objs = snaps[prm["ledger_index"]]
            ar = next((o for o in objs if o.get("Account") == prm["account"]), None)
            if ar is None:
                return {"result": {"status": "error", "error": "actNotFound"}}
            sls = [
                o
                for o in objs
                if o["LedgerEntryType"] == "SignerList" and o["index"] == P.signerlist_keylet(prm["account"])
            ]
            res = {"status": "success", "account_data": dict(ar)}
            if v2:
                res["signer_lists"] = sls
            else:
                res["account_data"]["signer_lists"] = sls
            return {"result": res}
        if m == "tx":
            t = txs[prm["transaction"]]
            return {
                "result": {
                    "status": "success",
                    "validated": True,
                    "ledger_index": t["ledger_index"],
                    "TransactionType": t["tx"]["TransactionType"],
                    "meta": {"TransactionResult": "tesSUCCESS"},
                }
            }
        raise AssertionError(m)

    return post


@pytest.fixture
def setup(tmp_path):
    a = write_snap(tmp_path / "a.gz", 10, SNAP_A)
    b = write_snap(tmp_path / "b.gz", 15, SNAP_B)
    t = write_tx(tmp_path / "tx", 10, 15, LINES)
    res = R.reconcile(a, b, t)
    assert res["result"] == "PASS"
    rj = tmp_path / "rec.json"
    rj.write_text(json.dumps(res))
    _, aa, sla = R.load_snapshot(a)
    _, ab, slb = R.load_snapshot(b)
    return res, R.postures(aa, sla), R.postures(ab, slb), str(rj), a, b


def rpc(post):
    return X.churn_rpc("http://x", gap=0, post=post, sleep=lambda s: None, methods=X.READ_ONLY_METHODS)


TXS = {x["tx"]["hash"]: x for x in LINES}


@pytest.mark.parametrize("v2", [False, True])
def test_all_ok(setup, v2):
    res, pa, pb, *_ = setup
    out = []
    rc = X.run(res, pa, pb, rpc(server({10: SNAP_A, 15: SNAP_B}, TXS, v2=v2)), 3, 1, out=out.append)
    assert rc == 0, out
    assert any("K1" in x and "absent" in x for x in out)  # deleted account answers actNotFound at B
    s = out[-1]
    assert "mismatches=0" in s and "effect_txs=6" in s


def test_negative_every_check_mismatches(setup):
    res, pa, pb, *_ = setup
    out = []
    rc = X.run(res, pa, pb, rpc(server({10: SNAP_A, 15: SNAP_B}, TXS)), 3, 1, negative=True, out=out.append)
    assert rc == 1
    lines = [x for x in out if x.startswith(("OK", "MISMATCH"))]
    assert lines and all(x.startswith("MISMATCH") for x in lines)


def test_server_differs(setup):
    res, pa, pb, *_ = setup
    b2 = [o if o.get("Account") != A0 else dict(o, RegularKey=A1) for o in SNAP_B]
    out = []
    assert X.run(res, pa, pb, rpc(server({10: SNAP_A, 15: b2}, TXS)), 3, 1, out=out.append) == 1
    assert sum(x.startswith("MISMATCH") for x in out) == 1


def test_retries_counted(setup):
    res, pa, pb, *_ = setup
    out = []
    assert (
        X.run(res, pa, pb, rpc(server({10: SNAP_A, 15: SNAP_B}, TXS, fail_first=2)), 3, 1, out=out.append)
        == 0
    )
    assert "retries=2" in out[-1] and "lgrNotFound" in out[-1]


def test_required_accounts(setup):
    res, pa, pb, *_ = setup
    must, sampled = X.pick_accounts(pa, pb, res["changed_accounts"], [A5], 20, 1)
    assert {A0, A1, A2, A4, A5} <= set(must) and not set(must) & set(sampled)


def test_main_refuses_failed_reconcile(setup, tmp_path):
    res, *_, a, b = setup
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(dict(res, result="FAIL")))
    assert X.main([str(bad), a, b]) == 2
    neg = tmp_path / "neg.json"
    neg.write_text(json.dumps(dict(res, negative="dropped x")))
    assert X.main([str(neg), a, b]) == 2
    assert X.main([setup[3], b, a]) == 2


def test_read_only_allowlist():
    with pytest.raises(ValueError, match="read-only"):
        rpc(lambda b: {}).call("submit", {})
