import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "acctchurn"))
import feesum_churn as fs  # noqa: E402


def led(li, n):
    return {"ledger_index": str(li), "ledger_hash": f"{li:064X}", "txn_count": str(n)}


def tx(li, i, fee, result="tesSUCCESS"):
    return {
        "ledger_index": str(li),
        "tx_index": str(i),
        "tx_hash": f"{li:032X}{i:032X}",
        "tx_type": "Payment",
        "result": result,
        "fee_drops": str(fee),
    }


LEDGERS = [led(9, 1), led(10, 2), led(11, 0), led(12, 1), led(13, 5)]
TXS = [tx(9, 0, 1000), tx(10, 0, 10), tx(10, 1, 12, "tecUNFUNDED_PAYMENT"), tx(12, 0, 15), tx(13, 0, 7)]


def test_sum_window_only_and_failed_tx_fees_count():
    s, lo, to = fs.feesum(LEDGERS, TXS, 10, 12)
    assert s["fee_sum_drops"] == 37  # 10 + 12 (tec still burns) + 15; ledgers 9 and 13 excluded
    assert (s["ledgers"], s["txs"], s["duplicate_tx_rows_dropped"]) == (3, 3, 0)
    assert [r[3] for r in lo] == [22, 0, 15]
    assert [r[0] for r in to] == [10, 10, 12]


def test_identical_duplicate_tx_row_counted_once():
    s, _, _ = fs.feesum(LEDGERS, TXS + [tx(10, 1, 12, "tecUNFUNDED_PAYMENT")], 10, 12)
    assert s["fee_sum_drops"] == 37
    assert s["duplicate_tx_rows_dropped"] == 1


def test_identical_duplicate_ledger_row_ok():
    s, _, _ = fs.feesum(LEDGERS + [led(11, 0)], TXS, 10, 12)
    assert s["ledgers"] == 3


def test_missing_ledger_fails():
    with pytest.raises(fs.FeeError, match="1 ledgers missing, first 11"):
        fs.feesum([r for r in LEDGERS if r["ledger_index"] != "11"], TXS, 10, 12)


def test_dropped_tx_row_fails():
    with pytest.raises(fs.FeeError, match="first 10: 1 rows vs txn_count 2"):
        fs.feesum(LEDGERS, [t for t in TXS if t["tx_index"] != "1"], 10, 12)


def test_conflicting_duplicate_tx_fails():
    with pytest.raises(fs.FeeError, match="conflicting duplicate rows"):
        fs.feesum(LEDGERS, TXS + [tx(10, 1, 99, "tecUNFUNDED_PAYMENT")], 10, 12)


def test_conflicting_duplicate_ledger_fails():
    with pytest.raises(fs.FeeError, match="ledger 11: conflicting duplicate rows"):
        fs.feesum(LEDGERS + [led(11, 3)], TXS, 10, 12)


def test_cli_writes_files_and_exit_codes(tmp_path):
    import csv
    import subprocess

    lp, tp = tmp_path / "ledgers.csv", tmp_path / "tx.csv"
    for path, rows in ((lp, LEDGERS), (tp, TXS)):
        with open(path, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
    script = os.path.join(os.path.dirname(__file__), "..", "acctchurn", "feesum_churn.py")
    ok = subprocess.run(
        [
            sys.executable,
            script,
            str(lp),
            str(tp),
            "--from",
            "10",
            "--to",
            "12",
            "--out-dir",
            str(tmp_path / "o"),
        ],
        capture_output=True,
        text=True,
    )
    assert ok.returncode == 0, ok.stderr
    assert '"fee_sum_drops": 37' in ok.stdout
    assert (tmp_path / "o" / "fees-tx.csv").read_text().count("\n") == 4
    bad = subprocess.run(
        [sys.executable, script, str(lp), str(tp), "--from", "10", "--to", "14"],
        capture_output=True,
        text=True,
    )
    assert bad.returncode == 2
    assert "ledgers missing, first 14" in bad.stderr
