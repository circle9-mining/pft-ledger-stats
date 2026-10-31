import importlib.util
import json
import os

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "_test_crosscheck_nft", os.path.join(HERE, "..", "nftflow", "crosscheck_nft.py")
)
cx = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cx)

ISS = "11" * 20
OWN = "22" * 20
A_ISS, A_OWN = cx.to_address(ISS), cx.to_address(OWN)


def nid(serial, issuer=ISS):
    return f"0008000A{issuer}00000001{serial:08X}"


N1, N2, N3, NB = nid(1), nid(2), nid(3), nid(4)
OFFER, GONE = "AB" * 32, "CD" * 32


def page(owner, ids):
    return {
        "LedgerEntryType": "NFTokenPage",
        "NFTokens": [{"NFToken": {"NFTokenID": i}} for i in ids],
    }, owner + "F" * 24


def objs(pages, roots, offers=()):
    out = {}
    for owner, ids in pages:
        o, idx = page(owner, ids)
        out[idx] = o
    for addr, minted, burned in roots:
        out["R" + addr] = {
            "LedgerEntryType": "AccountRoot",
            "Account": addr,
            "MintedNFTokens": minted,
            "BurnedNFTokens": burned,
        }
    for idx, o in offers:
        out[idx] = dict(o, LedgerEntryType="NFTokenOffer")
    return out


OFFER_OBJ = {"NFTokenID": N2, "Owner": A_OWN, "Amount": "10"}
OBJS_A = objs([(ISS, [N1, NB])], [(A_ISS, 2, 0)])
OBJS_B = objs([(ISS, [N1, N2]), (OWN, [N3])], [(A_ISS, 4, 1)], [(OFFER, OFFER_OBJ)])
LEDGER = {
    100: {"nfts": {A_ISS: [N1, NB]}, "roots": {A_ISS: (2, 0)}, "entries": {}},
    200: {
        "nfts": {A_ISS: [N1, N2], A_OWN: [N3]},
        "roots": {A_ISS: (4, 1)},
        "entries": {OFFER: OFFER_OBJ},
    },
}
TX = {
    "HM2": {"ledger_index": 150, "TransactionType": "NFTokenMint", "nftoken_id": N2},
    "HM3": {"ledger_index": 160, "TransactionType": "NFTokenMint", "nftoken_id": N3},
    "HB": {"ledger_index": 170, "TransactionType": "NFTokenBurn", "NFTokenID": NB},
    "HT": {"ledger_index": 180, "TransactionType": "NFTokenAcceptOffer", "nftoken_id": N3},
}


def ev(kind, n, h, frm, to):
    t = TX[h]
    return {
        "kind": kind,
        "nft_id": n,
        "tx_hash": h,
        "ledger_index": t["ledger_index"],
        "tx_type": t["TransactionType"],
        "from": frm,
        "to": to,
    }


RES = {
    "ledger_a": 100,
    "ledger_b": 200,
    "checks": [{"name": "R1", "pass": True, "detail": ""}],
    "negative_dropped": None,
    "counters": [{"issuer": ISS, "mints": 2, "burns": 1, "minted_delta": 2, "burned_delta": 1}],
    "events": [
        ev("mint", N2, "HM2", None, ISS),
        ev("mint", N3, "HM3", None, ISS),
        ev("burn", NB, "HB", ISS, None),
        ev("transfer", N3, "HT", ISS, OWN),
        {"kind": "offer_delete", "offer": GONE, "nft_id": N1, "ledger_index": 190},
    ],
}

FLAKY = {"left": 0, "error": "lgrNotFound"}
SEEN = []


def server(body):
    req = json.loads(body)
    SEEN.append(req["method"])
    if FLAKY["left"] > 0:
        FLAKY["left"] -= 1
        return {"result": {"status": "error", "error": FLAKY["error"]}}
    p = req["params"][0]
    m = req["method"]
    if m == "tx":
        t = dict(TX[p["transaction"]])
        nft = t.pop("nftoken_id", None)
        meta = {"TransactionResult": "tesSUCCESS"}
        if nft:
            meta["nftoken_id"] = nft
        return {"result": dict(t, validated=True, meta=meta, status="success")}
    led = LEDGER[p["ledger_index"]]
    if m == "account_nfts":
        ids = led["nfts"].get(p["account"])
        if ids is None:
            return {"result": {"status": "error", "error": "actNotFound"}}
        start = int(p.get("marker", 0))
        chunk = ids[start : start + 1]  # one per page, so paging is exercised
        res = {"status": "success", "account_nfts": [{"NFTokenID": i} for i in chunk]}
        if start + 1 < len(ids):
            res["marker"] = str(start + 1)
        return {"result": res}
    if m == "account_info":
        got = led["roots"].get(p["account"])
        if got is None:
            return {"result": {"status": "error", "error": "actNotFound"}}
        d = {"Account": p["account"], "MintedNFTokens": got[0], "BurnedNFTokens": got[1]}
        return {"result": {"status": "success", "account_data": d}}
    if m == "ledger_entry":
        node = led["entries"].get(p["index"])
        if node is None:
            return {"result": {"status": "error", "error": "entryNotFound"}}
        return {"result": {"status": "success", "node": node}}
    raise AssertionError(m)


def go(negative=False, res=RES, objs_b=OBJS_B, objs_a=OBJS_A):
    lines = []
    rpc = cx.churn_rpc("https://fake", gap=0, post=server, sleep=lambda s: None, methods=cx.READ_ONLY_METHODS)
    rc = cx.run(res, objs_a, objs_b, rpc, count=5, owners=3, seed=1, negative=negative, out=lines.append)
    return rc, lines


def test_all_ok():
    rc, lines = go()
    assert rc == 0, lines
    assert not [x for x in lines if x.startswith("MISMATCH")]
    summary = lines[-1]
    # 4 tx + 2 owners + 1 burn + 2 counters + 1 live offer + 1 deleted offer
    assert "checked=11 mismatches=0" in summary
    assert "owners=2/2" in summary and "offers_deleted=1/1" in summary


def test_negative_every_check_mismatches():
    rc, lines = go(negative=True)
    assert rc == 1
    checks = [x for x in lines if x.startswith(("OK", "MISMATCH"))]
    assert checks and all(x.startswith("MISMATCH") for x in checks)


def test_issuer_absent_at_a(monkeypatch):
    # an issuer account created in the window: no AccountRoot at A, account_info says actNotFound;
    # the negative control must still alter that expectation (live run 10 had 14 such X4 lines OK)
    monkeypatch.setitem(LEDGER[100], "roots", {})
    objs_a = objs([(ISS, [N1, NB])], [])
    rc, lines = go(objs_a=objs_a)
    assert rc == 0, lines
    assert any(x.startswith("OK X4") and "@A" in x and "expected=None got=None" in x for x in lines)
    rc, lines = go(negative=True, objs_a=objs_a)
    assert rc == 1
    checks = [x for x in lines if x.startswith(("OK", "MISMATCH"))]
    assert checks and all(x.startswith("MISMATCH") for x in checks), [x for x in checks if x.startswith("OK")]


def test_wrong_owner_set_detected():
    # snapshot B claims N3 still belongs to the issuer, the RPC says OWN
    tampered = objs([(ISS, [N1, N2, N3])], [(A_ISS, 4, 1)], [(OFFER, OFFER_OBJ)])
    rc, lines = go(objs_b=tampered)
    assert rc == 1
    assert any(x.startswith("MISMATCH X2") for x in lines)


def test_wrong_tx_id_detected():
    res = json.loads(json.dumps(RES))
    res["events"][0]["nft_id"] = N3  # mint tx HM2 created N2, not N3
    rc, lines = go(res=res)
    assert rc == 1
    assert any(x.startswith("MISMATCH X1 mint tx HM2") for x in lines)


def test_counter_drift_detected():
    tampered = objs([(ISS, [N1, N2]), (OWN, [N3])], [(A_ISS, 5, 1)], [(OFFER, OFFER_OBJ)])
    rc, lines = go(objs_b=tampered)
    assert rc == 1
    assert [x for x in lines if x.startswith("MISMATCH")] == [
        x for x in lines if x.startswith("MISMATCH X4") and "@B" in x
    ]


def test_retries_counted_not_mismatched():
    FLAKY.update(left=3, error="lgrNotFound")
    rc, lines = go()
    assert rc == 0
    assert 'retries=3 retries_by_error={"lgrNotFound": 3}' in lines[-1]


def test_persistent_rpc_error_raises():
    FLAKY.update(left=100, error="internal")
    with pytest.raises(cx.RpcError):
        go()
    FLAKY.update(left=0)


def test_only_read_only_methods():
    SEEN.clear()
    go()
    assert set(SEEN) <= cx.READ_ONLY_METHODS
    rpc = cx.churn_rpc("https://fake", gap=0, post=server, sleep=lambda s: None, methods=cx.READ_ONLY_METHODS)
    with pytest.raises(ValueError):
        rpc.call("submit", {})


def test_refuses_failed_or_negative_reconcile(tmp_path, capsys):
    for bad in (
        dict(RES, checks=[{"name": "R1", "pass": False, "detail": ""}]),
        dict(RES, negative_dropped=N2),
    ):
        p = tmp_path / "r.json"
        p.write_text(json.dumps(bad))
        assert cx.main([str(p), "a", "b"]) == 2
        assert "did not pass" in capsys.readouterr().out
