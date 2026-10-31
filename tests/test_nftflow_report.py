import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "nftflow"))
import report_nft as rp  # noqa: E402

ADDR = "rIssuerAddressThatIsLong123456"


def rec_ok():
    return {
        "ledger_a": 100,
        "ledger_b": 103,
        "negative_dropped": None,
        "residuals": [],
        "checks": [{"name": "R1 map", "pass": True, "detail": "2 live at B, 0 differences"}],
    }


def stats():
    page = {
        "pages": 1,
        "tokens": 2,
        "mean_fill": 2.0,
        "fill_ratio": 0.0625,
        "full_pages": 0,
        "owners": 1,
        "owners_multi_page": 0,
        "buckets": {"1-8": 1, "9-16": 0, "17-24": 0, "25-32": 0},
    }
    return {
        "window": {
            "ledger_a": 100,
            "ledger_b": 103,
            "close_a": "A",
            "close_b": "B",
            "nft_txs": 2,
            "live_a": 2,
            "live_b": 2,
            "mints": 1,
            "burns": 1,
            "transfers": 0,
            "page_moves": 0,
            "mints_with_uri": 1,
        },
        "mint_field_check": {"checked": 1, "mismatches": []},
        "hourly": [{"hour": 0, "mint": 1, "burn": 1, "transfer": 0, "offer_create": 0}],
        "issuers": [
            {
                "issuer": ADDR,
                "live_a": 2,
                "live_b": 2,
                "mints": 1,
                "burns": 1,
                "minted_delta": 1,
                "burned_delta": 1,
            }
        ],
        "taxons": {
            "population": 3,
            "distinct_issuer_taxon": 1,
            "top": [{"issuer": ADDR, "taxon": 0, "nfts": 3}],
        },
        "flags": {"transferable": 3},
        "transfer_fees": [{"fee": 0, "nfts": 3, "pct": "100.000%"}],
        "lifetime": {
            "minted_and_burned": {"n": 1, "min": 3700, "p50": 3700, "p90": 3700, "max": 3700, "mean": 3700.0},
            "burned_minted_before_a": 0,
            "minted_still_live_at_b": 0,
        },
        "offers": {
            "at_a": 0,
            "at_b": 0,
            "created": 0,
            "created_sell": 0,
            "created_buy": 0,
            "fates": {},
            "pre_window_offers_deleted": {},
            "create_to_accept_s": None,
        },
        "pages": {"a": page, "b": page},
    }


def write(tmp_path, rec, st):
    rj, sj = tmp_path / "r.json", tmp_path / "s.json"
    rj.write_text(json.dumps(rec))
    sj.write_text(json.dumps(st))
    return str(rj), str(sj)


def test_report_truncates_addresses_and_is_deterministic(tmp_path):
    rj, sj = write(tmp_path, rec_ok(), stats())
    xc = tmp_path / "x.log"
    xc.write_text("OK X1 foo\nSUMMARY checked=1 mismatches=0\n")
    out1, out2 = tmp_path / "1.md", tmp_path / "2.md"
    assert rp.main([rj, sj, "--out", str(out1), "--crosscheck", str(xc), "--tests", "1 passed"]) == 0
    assert rp.main([rj, sj, "--out", str(out2), "--crosscheck", str(xc), "--tests", "1 passed"]) == 0
    text = out1.read_text()
    assert text == out2.read_text()
    assert ADDR not in text and "`rIssuerA…`" in text
    assert "| R1 map | PASS |" in text
    assert "2 + mints 1 - burns 1 = 2 (snapshot B: 2)" in text
    assert "median 1h01m40s" in text
    assert "`SUMMARY checked=1 mismatches=0`" in text


def test_refuses_failed_negative_or_other_window(tmp_path, capsys):
    bad = rec_ok()
    bad["checks"][0]["pass"] = False
    assert rp.main([*write(tmp_path, bad, stats()), "--out", str(tmp_path / "o.md")]) == 2
    neg = rec_ok()
    neg["negative_dropped"] = "00AB"
    assert rp.main([*write(tmp_path, neg, stats()), "--out", str(tmp_path / "o.md")]) == 2
    res = rec_ok()
    res["residuals"] = ["R1 x"]
    assert rp.main([*write(tmp_path, res, stats()), "--out", str(tmp_path / "o.md")]) == 2
    other = stats()
    other["window"]["ledger_b"] = 104
    assert rp.main([*write(tmp_path, rec_ok(), other), "--out", str(tmp_path / "o.md")]) == 2
    assert not (tmp_path / "o.md").exists()
    assert "different windows" in capsys.readouterr().out
