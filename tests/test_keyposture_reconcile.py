import csv
import gzip
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "keyposture"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "state"))
import posture_key as P  # noqa: E402
import reconcile_key as R  # noqa: E402

from analyze import account_id_to_address  # noqa: E402

ADDR = [account_id_to_address(f"{i:040x}") for i in range(0x100, 0x110)]
ONE = "rrrrrrrrrrrrrrrrrrrrBZbvji"


def acct(a, flags=0, rk=None):
    o = {
        "LedgerEntryType": "AccountRoot",
        "Account": a,
        "Flags": flags,
        "Balance": "100",
        "index": P.account_keylet(a),
    }
    if rk:
        o["RegularKey"] = rk
    return o


def sl(owner, quorum, entries):
    return {
        "LedgerEntryType": "SignerList",
        "index": P.signerlist_keylet(owner),
        "SignerQuorum": quorum,
        "SignerEntries": [{"SignerEntry": {"Account": a, "SignerWeight": w}} for a, w in entries],
    }


def write_snap(path, idx, objs, paged=None):
    hdr = {
        "ledger_index": idx,
        "ledger_hash": f"H{idx}",
        "objects_paged": len(objs) if paged is None else paged,
    }
    with gzip.open(path, "wt") as fh:
        fh.write(json.dumps(hdr) + "\n")
        for o in objs:
            fh.write(json.dumps(o) + "\n")
    return str(path)


def write_tx(d, a_idx, b_idx, lines, gaps=(), skip=(), counts=None):
    os.makedirs(d, exist_ok=True)
    per = {}
    for x in lines:
        per[x["ledger_index"]] = per.get(x["ledger_index"], 0) + 1
    with open(os.path.join(d, "ledgers.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(
            [
                "ledger_index",
                "ledger_hash",
                "close_time",
                "close_time_iso",
                "txn_count",
                "key_tx_count",
                "fetched_utc",
            ]
        )
        for i in range(a_idx + 1, b_idx + 1):
            if i in skip or i in gaps:
                continue
            n = (counts or {}).get(i, per.get(i, 0))
            w.writerow([i, f"H{i}", 0, "", n, n, ""])
    if gaps:
        with open(os.path.join(d, "gaps.csv"), "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["ledger_index", "error", "logged_at"])
            for g in gaps:
                w.writerow([g, "x", ""])
    with open(os.path.join(d, "key-tx.jsonl"), "w") as fh:
        for x in lines:
            fh.write(json.dumps(x) + "\n")
    return str(d)


def line(li, ti, h, tt, nodes):
    return {
        "ledger_index": li,
        "tx_index": ti,
        "close_time": 0,
        "tx": {"hash": h, "TransactionType": tt},
        "meta": {"TransactionIndex": ti, "AffectedNodes": nodes},
    }


def mod_acct(a, prev, fin):
    return {
        "ModifiedNode": {
            "LedgerEntryType": "AccountRoot",
            "PreviousFields": prev,
            "FinalFields": dict(fin, Account=a),
        }
    }


A0, A1, A2, A3, A4, A5 = ADDR[:6]
SNAP_A = [
    acct(A0),
    acct(A1),
    acct(A2),
    acct(A3, rk=A0),
    sl(A3, 2, [(A0, 1), (A1, 1)]),
    {"LedgerEntryType": "Offer", "index": "O"},
]
# A0 gets a regular key, A1 a signer list, A2 disables master after a regular key, A4 is created,
# A3 (regular+signer list) is deleted
LINES = [
    line(11, 0, "T1", "SetRegularKey", [mod_acct(A0, {"Sequence": 1}, {"Flags": 0, "RegularKey": A5})]),
    line(
        11,
        1,
        "T2",
        "SignerListSet",
        [
            mod_acct(A1, {"OwnerCount": 0}, {"Flags": 0}),
            {
                "CreatedNode": {
                    "LedgerEntryType": "SignerList",
                    "LedgerIndex": P.signerlist_keylet(A1),
                    "NewFields": {
                        "SignerQuorum": 2,
                        "SignerEntries": [{"SignerEntry": {"Account": A5, "SignerWeight": 2}}],
                    },
                }
            },
        ],
    ),
    line(12, 0, "T3", "SetRegularKey", [mod_acct(A2, {"Sequence": 1}, {"Flags": 0, "RegularKey": A0})]),
    line(
        12,
        3,
        "T4",
        "AccountSet",
        [mod_acct(A2, {"Flags": 0}, {"Flags": P.LSF_DISABLE_MASTER, "RegularKey": A0})],
    ),
    line(
        13,
        0,
        "T5",
        "Payment",
        [{"CreatedNode": {"LedgerEntryType": "AccountRoot", "NewFields": {"Account": A4, "Balance": "10"}}}],
    ),
    line(
        14,
        0,
        "T6",
        "AccountDelete",
        [
            {
                "DeletedNode": {
                    "LedgerEntryType": "AccountRoot",
                    "FinalFields": {"Account": A3, "Flags": 0, "RegularKey": A0},
                }
            },
            {
                "DeletedNode": {
                    "LedgerEntryType": "SignerList",
                    "LedgerIndex": P.signerlist_keylet(A3),
                    "FinalFields": {},
                }
            },
        ],
    ),
]
SNAP_B = [
    acct(A0, rk=A5),
    acct(A1),
    sl(A1, 2, [(A5, 2)]),
    acct(A2, flags=P.LSF_DISABLE_MASTER, rk=A0),
    acct(A4),
]


@pytest.fixture
def world(tmp_path):
    def make(snap_b=SNAP_B, lines=LINES, **kw):
        a = write_snap(tmp_path / "a.gz", 10, SNAP_A)
        b = write_snap(tmp_path / "b.gz", 15, snap_b)
        t = write_tx(tmp_path / "tx", 10, 15, lines, **kw)
        return a, b, t

    return make


def test_clean_pass(world):
    r = R.reconcile(*world())
    assert r["result"] == "PASS", r["residuals"]
    assert r["created"] == 1 and r["deleted"] == 1 and r["key_tx_with_effect"] == 6
    pc = r["per_class"]
    assert pc["master_only"] == {"A": 3, "in": 1, "out": 3, "expected_B": 1, "B": 1}
    assert pc["master+regular"]["B"] == 1 and pc["master+multisig"]["B"] == 1 and pc["regular_only"]["B"] == 1
    assert pc["master+regular+multisig"]["out"] == 1
    assert sorted(r["changed_accounts"]) == sorted([A0, A1, A2, A3, A4])


def test_main_exit_codes(world, tmp_path):
    a, b, t = world()
    assert R.main(["--snap-a", a, "--snap-b", b, "--tx", t, "--json", str(tmp_path / "r.json")]) == 0
    assert R.main(["--snap-a", a, "--snap-b", b, "--tx", t, "--negative"]) == 1


def test_negative_drops_first_effect(world):
    r = R.reconcile(*world(), negative=True)
    assert r["result"] == "FAIL" and "T1" in r["negative"]
    assert any(x.startswith("R1 " + A0) for x in r["residuals"])


def test_negative_without_effects_tampers_a(world):
    a, b, t = world(
        snap_b=SNAP_A, lines=[line(11, 0, "N1", "AccountSet", [mod_acct(A0, {"Sequence": 1}, {"Flags": 0})])]
    )
    assert R.reconcile(a, b, t)["result"] == "PASS"
    r = R.reconcile(a, b, t, negative=True)
    assert r["result"] == "FAIL" and "flipped" in r["negative"]


def test_tampered_b_posture(world):
    b = [dict(o) for o in SNAP_B]
    b[0] = acct(A0, rk=A1)
    r = R.reconcile(*world(snap_b=b))
    assert any(x.startswith(f"R1 {A0}") for x in r["residuals"])


def test_class_count_and_untouched_change(world):
    b = SNAP_B + [acct(A5)]
    r = R.reconcile(*world(snap_b=b))
    assert any(x.startswith(f"R1 {A5}: in B") for x in r["residuals"])
    assert any(x.startswith("R2 class master_only") for x in r["residuals"])
    assert any(x.startswith(f"R5 {A5}") for x in r["residuals"])


def test_signerlist_set_mismatch(world):
    b = [o for o in SNAP_B if o["LedgerEntryType"] != "SignerList"]
    r = R.reconcile(*world(snap_b=b))
    assert any(x.startswith("R3 ") for x in r["residuals"])


def test_replay_order_not_file_order(world):
    r = R.reconcile(*world(lines=list(reversed(LINES))))
    assert r["result"] == "PASS", r["residuals"]


def test_r4_create_live_and_prev_mismatch(world):
    extra = [
        line(
            13,
            1,
            "X1",
            "Payment",
            [{"CreatedNode": {"LedgerEntryType": "AccountRoot", "NewFields": {"Account": A1}}}],
        ),
        line(13, 2, "X2", "SetRegularKey", [mod_acct(A1, {"RegularKey": A2}, {"Flags": 0})]),
        line(13, 3, "X3", "AccountSet", [mod_acct(A1, {"Flags": P.LSF_DISABLE_MASTER}, {"Flags": 0})]),
    ]
    r = R.reconcile(*world(lines=LINES + extra))
    res = " | ".join(r["residuals"])
    assert "already live" in res and "previous RegularKey" in res and "previous master flag" in res


def test_r4_modify_unknown_and_orphan_signerlist(world):
    extra = [
        line(13, 1, "Y1", "AccountSet", [mod_acct(A5, {"Flags": 0}, {"Flags": 0})]),
        line(
            13,
            2,
            "Y2",
            "SignerListSet",
            [{"CreatedNode": {"LedgerEntryType": "SignerList", "LedgerIndex": "FF" * 32, "NewFields": {}}}],
        ),
    ]
    r = R.reconcile(*world(lines=LINES + extra))
    res = " | ".join(r["residuals"])
    assert "not live" in res and "no live owner" in res


@pytest.mark.parametrize(
    "kw,msg",
    [
        ({"skip": (12,)}, "C1 ledgers.csv"),
        ({"gaps": (13,)}, "C1"),
        ({"counts": {11: 1}}, "C2"),
    ],
)
def test_incomplete_ledgers(world, kw, msg):
    with pytest.raises(R.Incomplete, match=msg):
        R.reconcile(*world(**kw))


def test_duplicate_hash(world):
    dup = LINES + [dict(LINES[0], tx_index=5)]
    with pytest.raises(R.Incomplete, match="C3"):
        R.reconcile(*world(lines=dup))


def test_malformed_jsonl(world):
    a, b, t = world()
    with open(os.path.join(t, "key-tx.jsonl"), "a") as fh:
        fh.write('{"ledger_index": 12, "tx": {}}\n')
    with pytest.raises(R.Incomplete, match="malformed"):
        R.reconcile(a, b, t)
    assert R.main(["--snap-a", a, "--snap-b", b, "--tx", t]) == 2


def test_bad_snapshots(tmp_path, world):
    a, b, t = world()
    with pytest.raises(R.Incomplete, match="header says"):
        R.reconcile(write_snap(tmp_path / "x.gz", 10, SNAP_A, paged=99), b, t)
    bad = [dict(SNAP_A[0], index="00" * 32)] + SNAP_A[1:]
    with pytest.raises(R.Incomplete, match="keylet"):
        R.reconcile(write_snap(tmp_path / "y.gz", 10, bad), b, t)
    orphan = SNAP_A + [sl(A5, 1, [(A0, 1)])]
    with pytest.raises(R.Incomplete, match="no live owner"):
        R.reconcile(write_snap(tmp_path / "z.gz", 10, orphan), b, t)
    with pytest.raises(R.Incomplete, match="not after"):
        R.reconcile(b, a, t)


def test_count_files(tmp_path, world):
    a, b, t = world()
    good = tmp_path / "c.json"
    good.write_text(json.dumps({"ledger_index": 10, "ledger_hash": "H10", "ok": True}))
    assert R.reconcile(a, b, t, count_a=str(good))["result"] == "PASS"
    bad = tmp_path / "d.json"
    bad.write_text(json.dumps({"ledger_index": 10, "ledger_hash": "H10", "ok": False}))
    with pytest.raises(R.Incomplete, match="C5"):
        R.reconcile(a, b, t, count_a=str(bad))
    other = tmp_path / "e.json"
    other.write_text(json.dumps({"ledger_index": 9, "ledger_hash": "H9", "ok": True}))
    with pytest.raises(R.Incomplete, match="C5"):
        R.reconcile(a, b, t, count_a=str(other))


@pytest.mark.parametrize(
    "master,rk,ms,cls",
    [
        (True, None, False, "master_only"),
        (True, A1, False, "master+regular"),
        (True, None, True, "master+multisig"),
        (True, A1, True, "master+regular+multisig"),
        (False, A1, False, "regular_only"),
        (False, None, True, "multisig_only"),
        (False, A1, True, "regular+multisig"),
        (False, ONE, False, "blackholed"),
        (False, None, False, "no_usable_key"),
    ],
)
def test_every_class(master, rk, ms, cls):
    p = {
        "master": master,
        "regular_key": rk,
        "signers": {"quorum": 2, "entries": [(A2, 1), (A3, 1)]} if ms else None,
    }
    assert P.classify(p) == cls


def test_weaknesses():
    s1 = {"quorum": 1, "entries": [(A2, 1)]}
    w = P.weaknesses(A0, {"master": True, "regular_key": A2, "signers": s1})
    assert set(w) == {
        "master_still_enabled",
        "single_signer_meets_quorum",
        "one_signer_entry",
        "regular_key_in_signer_list",
    }
    s2 = {"quorum": 5, "entries": [(A0, 1), (A2, 1)]}
    w = P.weaknesses(A0, {"master": False, "regular_key": None, "signers": s2})
    assert set(w) == {"quorum_unreachable", "self_in_signer_list"}
    assert P.weaknesses(A0, {"master": False, "regular_key": ONE, "signers": None}) == ["blackholed"]
    assert P.weaknesses(A0, {"master": False, "regular_key": None, "signers": None}) == ["no_usable_key"]
    assert P.weaknesses(A0, {"master": True, "regular_key": None, "signers": None}) == []
    assert (
        P.weaknesses(
            A0, {"master": False, "regular_key": A2, "signers": {"quorum": 3, "entries": [(A1, 2), (A3, 2)]}}
        )
        == []
    )


def test_decode_address_errors():
    assert P.decode_address(ONE) == b"\x00" * 19 + b"\x01"
    with pytest.raises(ValueError, match="character"):
        P.decode_address("r0OIl")
    with pytest.raises(ValueError, match="checksum"):
        P.decode_address(A0[:-1] + ("r" if A0[-1] != "r" else "p"))


def test_signer_summary_sorted():
    s = P.signer_summary(sl(A0, 3, [(A2, 1), (A1, 2)]))
    assert s == {"quorum": 3, "entries": sorted([(A1, 2), (A2, 1)])}
