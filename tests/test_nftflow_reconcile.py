import csv
import gzip
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "nftflow"))
import collect_nft as col  # noqa: E402
import reconcile_nft as rec  # noqa: E402

ISS = "11" * 20
OWN = "22" * 20
ISS_ADDR = rec.account_id_to_address(ISS)
OWN_ADDR = rec.account_id_to_address(OWN)
A, B = 100, 103


def nid(n, issuer=ISS, flags=8, fee=0):
    return f"{flags:04X}{fee:04X}{issuer}{0:08X}{n:08X}"


def page_index(owner):
    return owner + "FF" * 12


def toks(*ns):
    return [{"NFToken": {"NFTokenID": nid(n), "URI": "AB"}} for n in ns]


def page(owner, *ns):
    return {"index": page_index(owner), "LedgerEntryType": "NFTokenPage", "Flags": 0, "NFTokens": toks(*ns)}


def acct(addr, n, minted=None, burned=None):
    o = {
        "index": f"{n:064X}",
        "LedgerEntryType": "AccountRoot",
        "Account": addr,
        "Balance": "1000",
        "Sequence": 1,
        "OwnerCount": 0,
    }
    if minted is not None:
        o["MintedNFTokens"] = minted
    if burned is not None:
        o["BurnedNFTokens"] = burned
    return o


OFFER = "0F" * 32


def write_snap(path, ledger, objs, paged=None, close_iso="2026-10-30T00:00:00Z"):
    header = {
        "format": "pft-state-snapshot/1",
        "ledger_index": ledger,
        "ledger_hash": "AB" * 32,
        "close_time_iso": close_iso,
        "total_coins": 1,
        "objects_paged": len(objs) if paged is None else paged,
    }
    with gzip.open(path, "wt") as fh:
        fh.write(json.dumps(header) + "\n")
        for o in objs:
            fh.write(json.dumps(o) + "\n")
    return str(path)


def mod_page(owner, prev, final):
    return {
        "ModifiedNode": {
            "LedgerEntryType": "NFTokenPage",
            "LedgerIndex": page_index(owner),
            "PreviousFields": {"NFTokens": toks(*prev)},
            "FinalFields": {"NFTokens": toks(*final)},
        }
    }


def txrec(ledger, i, tt, nodes, h):
    return {
        "ledger_index": ledger,
        "close_time": 1000 + ledger,
        "close_time_iso": "x",
        "tx_index": i,
        "tx": {"TransactionType": tt, "hash": h, "Account": ISS_ADDR},
        "meta": {"TransactionResult": "tesSUCCESS", "TransactionIndex": i, "AffectedNodes": nodes},
    }


def scenario_txs():
    mint = txrec(101, 0, "NFTokenMint", [mod_page(ISS, [1, 2], [1, 2, 3])], "H1")
    burn = txrec(102, 0, "NFTokenBurn", [mod_page(ISS, [1, 2, 3], [2, 3])], "H2")
    offer = txrec(
        102,
        1,
        "NFTokenCreateOffer",
        [
            {
                "CreatedNode": {
                    "LedgerEntryType": "NFTokenOffer",
                    "LedgerIndex": OFFER,
                    "NewFields": {"NFTokenID": nid(2), "Owner": ISS_ADDR, "Flags": 1, "Amount": "0"},
                }
            }
        ],
        "H3",
    )
    accept = txrec(
        103,
        0,
        "NFTokenAcceptOffer",
        [
            mod_page(ISS, [2, 3], [3]),
            {
                "CreatedNode": {
                    "LedgerEntryType": "NFTokenPage",
                    "LedgerIndex": page_index(OWN),
                    "NewFields": {"NFTokens": toks(2)},
                }
            },
            {
                "DeletedNode": {
                    "LedgerEntryType": "NFTokenOffer",
                    "LedgerIndex": OFFER,
                    "FinalFields": {"NFTokenID": nid(2), "Owner": ISS_ADDR, "Flags": 1},
                }
            },
        ],
        "H4",
    )
    return [mint, burn, offer, accept]


def write_tx(d, recs, ledgers=None, gaps=0, count_override=None):
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "nft-tx.jsonl"), "w") as fh:
        for r in recs:
            fh.write(json.dumps(r) + "\n")
    ledgers = list(range(A + 1, B + 1)) if ledgers is None else ledgers
    with open(os.path.join(d, "ledgers.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=col.LEDGER_FIELDS)
        w.writeheader()
        for li in ledgers:
            n = sum(1 for r in recs if r["ledger_index"] == li)
            if count_override and li in count_override:
                n = count_override[li]
            w.writerow(
                {
                    "ledger_index": li,
                    "ledger_hash": "CD" * 32,
                    "close_time": 1,
                    "close_time_iso": "x",
                    "txn_count": n,
                    "nft_tx_count": n,
                    "fetched_utc": "x",
                }
            )
    if gaps:
        with open(os.path.join(d, "gaps.csv"), "w") as fh:
            fh.write("ledger_index,error,logged_at\n")
            for i in range(gaps):
                fh.write(f"{A + 1 + i},LookupError,x\n")
    return str(d)


@pytest.fixture
def world(tmp_path):
    sa = write_snap(tmp_path / "a.jsonl.gz", A, [page(ISS, 1, 2), acct(ISS_ADDR, 1, 2, 0), acct(OWN_ADDR, 2)])
    b_objs = [page(ISS, 3), page(OWN, 2), acct(ISS_ADDR, 1, 3, 1), acct(OWN_ADDR, 2)]
    sb = write_snap(tmp_path / "b.jsonl.gz", B, b_objs)
    tx = write_tx(tmp_path / "tx", scenario_txs())
    return tmp_path, sa, sb, tx, b_objs


def run(sa, sb, tx, *extra):
    return rec.main(["--snap-a", sa, "--snap-b", sb, "--tx", tx, *extra])


def test_clean_reconciles(world, capsys):
    tmp, sa, sb, tx, _ = world
    assert run(sa, sb, tx, "--json", str(tmp / "r.json")) == 0
    r = json.load(open(tmp / "r.json"))
    assert (r["mints"], r["burns"], r["transfers"], r["live_a"], r["live_b"]) == (1, 1, 1, 2, 2)
    assert r["residuals"] == [] and all(c["pass"] for c in r["checks"])
    kinds = [e["kind"] for e in r["events"]]
    assert kinds.count("offer_create") == 1 and kinds.count("offer_delete") == 1
    assert "RESULT: PASS" in capsys.readouterr().out


def test_negative_control_fails(world, capsys):
    _, sa, sb, tx, _ = world
    assert run(sa, sb, tx, "--negative") == 1
    out = capsys.readouterr().out
    assert "NEGATIVE CONTROL: dropped mint " + nid(3) in out
    assert "FAIL R1" in out and "FAIL R2" in out and "FAIL R3" in out


def test_tampered_owner_in_b(world, tmp_path):
    _, sa, _, tx, b_objs = world
    objs = [page(ISS, 2, 3), acct(ISS_ADDR, 1, 3, 1), acct(OWN_ADDR, 2)]
    sb = write_snap(tmp_path / "b2.jsonl.gz", B, objs)
    assert run(sa, sb, tx) == 1


def test_tampered_counter_in_b(world, tmp_path, capsys):
    _, sa, _, tx, _ = world
    objs = [page(ISS, 3), page(OWN, 2), acct(ISS_ADDR, 1, 3, 0), acct(OWN_ADDR, 2)]
    sb = write_snap(tmp_path / "b3.jsonl.gz", B, objs)
    assert run(sa, sb, tx) == 1
    assert "BurnedNFTokens delta 0 != burns 1" in capsys.readouterr().out


def test_tampered_offer_set(world, tmp_path):
    _, sa, _, tx, b_objs = world
    extra = {"index": OFFER, "LedgerEntryType": "NFTokenOffer", "NFTokenID": nid(2), "Owner": ISS_ADDR}
    sb = write_snap(tmp_path / "b4.jsonl.gz", B, b_objs + [extra])
    assert run(sa, sb, tx) == 1


def test_damaged_snapshot_count(world, tmp_path, capsys):
    _, sa, _, tx, b_objs = world
    sb = write_snap(tmp_path / "b5.jsonl.gz", B, b_objs, paged=len(b_objs) + 1)
    assert run(sa, sb, tx) == 2
    assert "INCOMPLETE" in capsys.readouterr().out


def test_missing_ledger_is_incomplete(world, tmp_path, capsys):
    _, sa, sb, _, _ = world
    tx = write_tx(tmp_path / "tx2", scenario_txs(), ledgers=[101, 103])
    assert run(sa, sb, tx) == 2
    assert "1 ledgers missing" in capsys.readouterr().out


def test_gap_file_is_incomplete(world, tmp_path):
    _, sa, sb, _, _ = world
    assert run(sa, sb, write_tx(tmp_path / "tx3", scenario_txs(), gaps=1)) == 2


def test_duplicate_tx_is_incomplete(world, tmp_path, capsys):
    _, sa, sb, _, _ = world
    recs = scenario_txs()
    recs.insert(1, recs[0])
    assert run(sa, sb, write_tx(tmp_path / "tx4", recs, count_override={101: 2})) == 2
    assert "appear twice" in capsys.readouterr().out


def test_count_mismatch_is_incomplete(world, tmp_path, capsys):
    _, sa, sb, _, _ = world
    recs = scenario_txs()
    tx = write_tx(tmp_path / "tx5", recs, count_override={102: 3})
    assert run(sa, sb, tx) == 2
    assert "nft_tx_count != jsonl lines" in capsys.readouterr().out


def test_duplicate_ledger_row_is_incomplete(world, tmp_path):
    _, sa, sb, _, _ = world
    assert run(sa, sb, write_tx(tmp_path / "tx6", scenario_txs(), ledgers=[101, 102, 102, 103])) == 2


def test_page_split_is_a_move_not_a_mint():
    other = ISS + "00" * 12
    meta = {
        "AffectedNodes": [
            mod_page(ISS, [1, 2], [2, 3]),
            {
                "CreatedNode": {
                    "LedgerEntryType": "NFTokenPage",
                    "LedgerIndex": other,
                    "NewFields": {"NFTokens": toks(1)},
                }
            },
        ]
    }
    effects, _, _ = rec.tx_effects(meta)
    assert sorted((k, n) for k, n, *_ in effects) == [("mint", nid(3)), ("move", nid(1)), ("move", nid(2))]


def test_modified_page_without_nftokens_change_is_ignored():
    meta = {
        "AffectedNodes": [
            {
                "ModifiedNode": {
                    "LedgerEntryType": "NFTokenPage",
                    "LedgerIndex": page_index(ISS),
                    "PreviousFields": {"NextPageMin": "00"},
                    "FinalFields": {"NFTokens": toks(1)},
                }
            }
        ]
    }
    assert rec.tx_effects(meta)[0] == []


def test_deleted_page_burn():
    meta = {
        "AffectedNodes": [
            {
                "DeletedNode": {
                    "LedgerEntryType": "NFTokenPage",
                    "LedgerIndex": page_index(ISS),
                    "PreviousFields": {"NFTokens": toks(5)},
                    "FinalFields": {"NFTokens": []},
                }
            }
        ]
    }
    assert [(k, n) for k, n, *_ in rec.tx_effects(meta)[0]] == [("burn", nid(5))]


def test_account_hex_roundtrip():
    assert rec._account_hex(ISS_ADDR) == ISS
    assert rec.issuer_of(nid(1)) == ISS and rec.flags_of(nid(1)) == 8 and rec.serial_of(nid(7)) == 7


def test_collector_orphan_and_torn_repair(tmp_path):
    p = tmp_path / "nft-tx.jsonl"
    lines = [json.dumps({"ledger_index": li}) for li in (5, 6, 7, 7)]
    p.write_text("\n".join(lines) + "\n" + '{"ledger_in')
    assert col.drop_torn_tail(str(p)) == len('{"ledger_in')
    assert col.drop_orphan_lines(str(p), 6) == 2
    assert [json.loads(x)["ledger_index"] for x in p.read_text().splitlines()] == [5, 6]
    assert col.drop_orphan_lines(str(p), 6) == 0


def test_collector_selects_nft_txs_only():
    led = {
        "ledger_index": "9",
        "close_time": 1,
        "transactions": [
            {
                "TransactionType": "Payment",
                "hash": "P",
                "metaData": {"TransactionIndex": 0, "AffectedNodes": []},
            },
            {
                "TransactionType": "NFTokenBurn",
                "hash": "N",
                "metaData": {"TransactionIndex": 2, "AffectedNodes": []},
            },
            {
                "TransactionType": "AccountDelete",
                "hash": "D",
                "metaData": {
                    "TransactionIndex": 1,
                    "AffectedNodes": [
                        {
                            "DeletedNode": {
                                "LedgerEntryType": "NFTokenOffer",
                                "LedgerIndex": OFFER,
                                "FinalFields": {},
                            }
                        }
                    ],
                },
            },
        ],
    }
    lines, n = col.nft_lines(led)
    assert n == 3 and [x["tx"]["hash"] for x in lines] == ["D", "N"]
    assert "metaData" not in lines[0]["tx"]


def _objs(*objs):
    return {o["index"]: o for o in objs}


def _hdr(ledger):
    return {"ledger_index": ledger, "close_time_iso": "x"}


def test_mint_of_live_id_is_a_residual():
    # ID 1 is live on the issuer's page at A; a tx "mints" it again onto another owner's new page
    objs_a = _objs(page(ISS, 1), acct(ISS_ADDR, 1, 1, 0))
    objs_b = _objs(page(OWN, 1), acct(ISS_ADDR, 1, 2, 0))
    created = {
        "CreatedNode": {
            "LedgerEntryType": "NFTokenPage",
            "LedgerIndex": page_index(OWN),
            "NewFields": {"NFTokens": toks(1)},
        }
    }
    res = rec.reconcile(_hdr(A), objs_a, _hdr(B), objs_b, [txrec(101, 0, "NFTokenMint", [created], "HX")])
    assert any(s.startswith("R1 mint of live " + nid(1)) for s in res["residuals"])


def test_burn_by_wrong_owner_is_a_residual():
    # ID 1 is live with the issuer, but the burn removes it from OWN's page
    objs_a = _objs(page(ISS, 1, 2), acct(ISS_ADDR, 1, 2, 0))
    objs_b = _objs(page(ISS, 1, 2), acct(ISS_ADDR, 1, 2, 1))
    burn = txrec(101, 0, "NFTokenBurn", [mod_page(OWN, [1], [])], "HX")
    res = rec.reconcile(_hdr(A), objs_a, _hdr(B), objs_b, [burn])
    assert any(s.startswith(f"R1 burn of {nid(1)} not live with owner {OWN}") for s in res["residuals"])


def test_replay_uses_transaction_index_order(tmp_path, capsys):
    # mint (index 0) and burn (index 1) of ID 3 in one ledger, written to the file in reverse order
    sa = write_snap(tmp_path / "a.jsonl.gz", A, [page(ISS, 1, 2), acct(ISS_ADDR, 1, 2, 0)])
    sb = write_snap(tmp_path / "b.jsonl.gz", B, [page(ISS, 1, 2), acct(ISS_ADDR, 1, 3, 1)])
    mint = txrec(101, 0, "NFTokenMint", [mod_page(ISS, [1, 2], [1, 2, 3])], "HM")
    burn = txrec(101, 1, "NFTokenBurn", [mod_page(ISS, [1, 2, 3], [1, 2])], "HB")
    tx = write_tx(tmp_path / "tx", [burn, mint])
    assert run(sa, sb, tx) == 0, capsys.readouterr().out


def test_remint_of_an_id_live_at_a_fails_r5(tmp_path, capsys):
    # ID 1 burned and then minted again: owner map, supply and counters all balance, only R5 sees it
    sa = write_snap(tmp_path / "a.jsonl.gz", A, [page(ISS, 1, 2), acct(ISS_ADDR, 1, 2, 0)])
    sb = write_snap(tmp_path / "b.jsonl.gz", B, [page(ISS, 1, 2), acct(ISS_ADDR, 1, 3, 1)])
    burn = txrec(101, 0, "NFTokenBurn", [mod_page(ISS, [1, 2], [2])], "HB")
    mint = txrec(102, 0, "NFTokenMint", [mod_page(ISS, [2], [1, 2])], "HM")
    tx = write_tx(tmp_path / "tx", [burn, mint])
    assert run(sa, sb, tx) == 1
    out = capsys.readouterr().out
    assert "FAIL R5" in out and f"RESIDUAL R5 NFTokenID {nid(1)} minted more than once" in out
    assert "PASS R1" in out and "PASS R2" in out and "PASS R3" in out
