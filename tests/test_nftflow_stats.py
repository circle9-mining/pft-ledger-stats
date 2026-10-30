import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "nftflow"))
import reconcile_nft as rec  # noqa: E402
import stats_nft as st  # noqa: E402
from test_nftflow_reconcile import (  # noqa: E402
    ISS,
    ISS_ADDR,
    OWN_ADDR,
    A,
    B,
    acct,
    page,
    scenario_txs,
    write_snap,
    write_tx,
)

A_ISO, B_ISO = "2000-01-01T00:18:20Z", "2000-01-01T00:18:23Z"


def scrambled(taxon, seq):
    return taxon ^ ((384160001 * seq + 2459) & 0xFFFFFFFF)


def mint_fields(recs, taxon=None):
    """Give the scenario's mint tx the fields of the NFTokenID it mints (nid(3): flags 8, fee 0)."""
    for r in recs:
        if r["tx"]["TransactionType"] == "NFTokenMint":
            r["tx"].update({"Flags": 8, "NFTokenTaxon": scrambled(0, 3) if taxon is None else taxon})
    return recs


@pytest.fixture
def world(tmp_path):
    # scenario tx close_time is 1000 + ledger (Ripple epoch) -> 2000-01-01T00:18:20Z + (ledger - 100) s
    a_objs = [page(ISS, 1, 2), acct(ISS_ADDR, 1, 2, 0), acct(OWN_ADDR, 2)]
    b_objs = [page(ISS, 3), page("22" * 20, 2), acct(ISS_ADDR, 1, 3, 1), acct(OWN_ADDR, 2)]
    sa = write_snap(tmp_path / "a.jsonl.gz", A, a_objs, close_iso=A_ISO)
    sb = write_snap(tmp_path / "b.jsonl.gz", B, b_objs, close_iso=B_ISO)
    tx = write_tx(tmp_path / "tx", mint_fields(scenario_txs()))
    rj = str(tmp_path / "r.json")
    assert rec.main(["--snap-a", sa, "--snap-b", sb, "--tx", tx, "--json", rj]) == 0
    return tmp_path, sa, sb, tx, rj


def run(sa, sb, tx, rj, *extra):
    return st.main(["--reconcile", rj, "--snap-a", sa, "--snap-b", sb, "--tx", tx, *extra])


def test_taxon_unscramble_roundtrip():
    for taxon, seq in [(0, 0), (7, 1), (0xFFFFFFFF, 12345), (42, 0xFFFFFFFF)]:
        n = f"0008000A{ISS}{scrambled(taxon, seq):08X}{seq:08X}"
        assert st.taxon_of(n) == taxon
        assert (rec.flags_of(n), rec.fee_of(n), rec.serial_of(n)) == (8, 10, seq)


def test_flag_labels():
    assert st.flag_label(0) == "none"
    assert st.flag_label(9) == "burnable+transferable"
    assert st.flag_label(0x8008) == "transferable+0x8000"


def test_clean_stats(world, capsys):
    tmp, sa, sb, tx, rj = world
    assert run(sa, sb, tx, rj, "--json", str(tmp / "s.json"), "--chart", str(tmp / "c.png")) == 0
    s = json.load(open(tmp / "s.json"))
    w = s["window"]
    assert (w["mints"], w["burns"], w["transfers"], w["mints_with_uri"]) == (1, 1, 1, 1)
    assert s["mint_field_check"] == {"checked": 1, "mismatches": []}
    assert s["lifetime"] == {
        "minted_and_burned": None,
        "burned_minted_before_a": 1,
        "minted_still_live_at_b": 1,
    }
    o = s["offers"]
    assert (o["created"], o["created_sell"], o["fates"]) == (1, 1, {"accepted": 1})
    assert o["create_to_accept_s"]["max"] == 1
    assert s["issuers"] == [
        {
            "issuer": ISS_ADDR,
            "live_a": 2,
            "live_b": 2,
            "mints": 1,
            "burns": 1,
            "minted_delta": 1,
            "burned_delta": 1,
        }
    ]
    assert s["flags"] == {"transferable": 2}
    assert s["pages"]["a"]["pages"] == 1 and s["pages"]["b"]["pages"] == 2
    assert s["pages"]["b"]["mean_fill"] == 1.0
    assert sum(r["mint"] for r in s["hourly"]) == 1
    assert os.path.getsize(tmp / "c.png") > 1000
    assert "RESULT: PASS" in capsys.readouterr().out


def test_output_is_deterministic(world):
    tmp, sa, sb, tx, rj = world
    for name in ("s1.json", "s2.json"):
        assert run(sa, sb, tx, rj, "--json", str(tmp / name)) == 0
    assert open(tmp / "s1.json").read() == open(tmp / "s2.json").read()


def test_mint_field_mismatch_fails(tmp_path, world, capsys):
    tmp, sa, sb, _, _ = world
    tx = write_tx(tmp_path / "tx2", mint_fields(scenario_txs(), taxon=5))
    rj = str(tmp_path / "r2.json")
    assert rec.main(["--snap-a", sa, "--snap-b", sb, "--tx", tx, "--json", rj]) == 0
    assert run(sa, sb, tx, rj) == 1
    out = capsys.readouterr().out
    assert f"MISMATCH H1: taxon tx 5 != NFTokenID {scrambled(0, 3)}" in out and "RESULT: FAIL" in out


def test_refuses_failed_or_negative_reconcile(world, capsys):
    tmp, sa, sb, tx, _ = world
    rj = str(tmp / "neg.json")
    assert rec.main(["--snap-a", sa, "--snap-b", sb, "--tx", tx, "--json", rj, "--negative"]) == 1
    assert run(sa, sb, tx, rj) == 2
    assert "reconciliation did not pass" in capsys.readouterr().out


def test_refuses_other_window(world, tmp_path, capsys):
    tmp, sa, _, tx, rj = world
    other = write_snap(tmp_path / "b2.jsonl.gz", B + 1, [page(ISS, 3)], close_iso=B_ISO)
    assert run(sa, other, tx, rj) == 2
    assert "!= reconcile" in capsys.readouterr().out


def test_tx_dir_changed_after_reconcile(world, capsys):
    tmp, sa, sb, tx, rj = world
    with open(os.path.join(tx, "gaps.csv"), "w") as fh:
        fh.write(f"ledger_index,error,logged_at\n{A + 1},LookupError,x\n")
    assert run(sa, sb, tx, rj) == 2
    assert "INCOMPLETE: gaps.csv lists 1 ledgers" in capsys.readouterr().out


def test_event_before_snapshot_a_is_refused(world, tmp_path, capsys):
    tmp, _, sb, tx, rj = world
    late_a = write_snap(tmp_path / "a2.jsonl.gz", A, [page(ISS, 1, 2)], close_iso="2026-10-30T00:00:00Z")
    assert run(late_a, sb, tx, rj) == 2
    assert "outside snapshot A close" in capsys.readouterr().out


def test_page_fill_buckets():
    pf = st.page_fill([("a", 32), ("a", 1), ("b", 9), ("c", 16)])
    assert pf["buckets"] == {"1-8": 1, "9-16": 2, "25-32": 1}
    assert (pf["full_pages"], pf["owners"], pf["owners_multi_page"], pf["tokens"]) == (1, 3, 1, 58)
    assert st.quantiles([3, 1, 2]) == {"n": 3, "min": 1, "p50": 2, "p90": 3, "max": 3, "mean": 2.0}
