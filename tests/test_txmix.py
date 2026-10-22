import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "txmix"))

from analyze_tx import analyze, dedupe, gini, main, percentile  # noqa: E402
from collect_tx import accounts_created, delivered_class  # noqa: E402

FIELDS = ["ledger_index", "close_time", "close_time_iso", "tx_index", "tx_hash", "tx_type", "result",
          "fee_drops", "account", "memo", "signers", "delivered_class", "accounts_created"]


def tx(i, t, acct, fee=10, result="tesSUCCESS", memo="n", signers=0, created=0, hour=10):
    return {"ledger_index": str(1000 + i), "close_time": str(i),
            "close_time_iso": f"2026-10-22T{hour:02d}:00:00Z", "tx_index": "0",
            "tx_hash": f"H{i:04d}", "tx_type": t, "result": result, "fee_drops": str(fee),
            "account": acct, "memo": memo, "signers": str(signers), "delivered_class": "none",
            "accounts_created": str(created)}


def synthetic():
    """100 tx: 60 Payment, 30 NFTokenMint, 10 OfferCreate; fees 1..100 drops; known accounts."""
    rows = []
    for i in range(100):
        t = "Payment" if i < 60 else ("NFTokenMint" if i < 90 else "OfferCreate")
        res = "tecUNFUNDED_OFFER" if t == "OfferCreate" and i % 2 == 0 else "tesSUCCESS"
        acct = "aaaa" if i < 70 else f"acct{i:02d}"            # 1 account sends 70, 30 send 1 each
        rows.append(tx(i, t, acct, fee=i + 1, result=res, memo="y" if i < 25 else "n",
                       signers=2 if i == 99 else 0, created=1 if i < 5 else 0, hour=10 if i < 50 else 11))
    ledgers = [{"ledger_index": str(1000 + i), "close_time_iso": r["close_time_iso"],
                "txn_count": "1"} for i, r in enumerate(rows)]
    return rows, ledgers


class TestPrimitives(unittest.TestCase):
    def test_percentile_nearest_rank(self):
        vals = list(range(1, 101))
        self.assertEqual(percentile(vals, 50), 50)
        self.assertEqual(percentile(vals, 90), 90)
        self.assertEqual(percentile(vals, 99), 99)
        self.assertEqual(percentile(vals, 100), 100)
        self.assertEqual(percentile([7], 99), 7)
        self.assertIsNone(percentile([], 50))

    def test_gini_known_values(self):
        self.assertAlmostEqual(gini([5, 5, 5, 5]), 0.0)
        self.assertAlmostEqual(gini([0, 0, 0, 1]), 0.75)
        self.assertAlmostEqual(gini([1, 2, 3, 4]), 0.25)
        self.assertEqual(gini([]), 0.0)

    def test_dedupe_by_hash(self):
        r = tx(1, "Payment", "a")
        self.assertEqual(len(dedupe([r, dict(r), tx(2, "Payment", "a")])), 2)

    def test_collector_classifiers(self):
        self.assertEqual(delivered_class({}), "none")
        self.assertEqual(delivered_class({"delivered_amount": "1000"}), "native")
        self.assertEqual(delivered_class({"delivered_amount": {"currency": "USD", "value": "1"}}), "issued")
        self.assertEqual(delivered_class({"delivered_amount": "unavailable"}), "unavailable")
        meta = {"AffectedNodes": [{"CreatedNode": {"LedgerEntryType": "AccountRoot"}},
                                  {"CreatedNode": {"LedgerEntryType": "Offer"}},
                                  {"ModifiedNode": {"LedgerEntryType": "AccountRoot"}}]}
        self.assertEqual(accounts_created(meta), 1)


class TestPositiveControl(unittest.TestCase):
    def setUp(self):
        rows, self.ledgers = synthetic()
        rows.append(dict(rows[3]))                      # duplicate row must be dropped
        self.a = analyze(dedupe(rows), self.ledgers, base_fee=10)

    def test_mix(self):
        self.assertEqual(self.a["n"], 100)
        self.assertEqual(dict(self.a["types"]), {"Payment": 60, "NFTokenMint": 30, "OfferCreate": 10})
        self.assertEqual(dict(self.a["by_hour"]["2026-10-22T10"]), {"Payment": 50})
        self.assertEqual(dict(self.a["by_hour"]["2026-10-22T11"]),
                         {"Payment": 10, "NFTokenMint": 30, "OfferCreate": 10})

    def test_result_classes(self):
        self.assertEqual(self.a["classes"]["OfferCreate"], {"tec": 5, "tes": 5})
        self.assertEqual(self.a["results"]["OfferCreate"]["tecUNFUNDED_OFFER"], 5)
        self.assertEqual(self.a["classes"]["Payment"], {"tes": 60})

    def test_fees(self):
        f = self.a["fee"]
        self.assertEqual((f["p50"], f["p90"], f["p99"], f["max"], f["min"]), (50, 90, 99, 100, 1))
        self.assertEqual(f["above_10x"], 0)             # 100 is not > 10 * 10
        self.assertEqual(f["at_base"], 1)
        self.assertEqual(f["total"], 5050)
        a = analyze([tx(1, "Payment", "a", fee=101), tx(2, "Payment", "a", fee=10)], self.ledgers[:2], 10)
        self.assertEqual(a["fee"]["above_10x"], 1)

    def test_accounts_gini_top10(self):
        self.assertEqual(self.a["accounts"], 31)
        self.assertAlmostEqual(self.a["top10_share"], 0.79)  # 70 + 9 * 1
        # Independent definition: G = sum_ij |xi - xj| / (2 n^2 mean). x = [1]*30 + [70]:
        # 2 * 30 pairs differ by 69 -> 4140 / (2 * 31 * 100) = 0.66774
        xs = [1] * 30 + [70]
        mad = sum(abs(x - y) for x in xs for y in xs) / (2 * len(xs) ** 2 * (sum(xs) / len(xs)))
        self.assertAlmostEqual(self.a["gini"], mad)
        self.assertAlmostEqual(self.a["gini"], 4140 / 6200)

    def test_memo_multisig_created(self):
        self.assertEqual(self.a["memo"], 25)
        self.assertEqual(self.a["multisig"], 1)
        self.assertEqual(self.a["created_total"], 5)
        self.assertEqual(dict(self.a["created"]), {"Payment": 5})


class TestEndToEnd(unittest.TestCase):
    def test_report_written(self):
        import csv
        rows, ledgers = synthetic()
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "tx.csv"), "w", newline="") as f:
                w = csv.DictWriter(f, FIELDS)
                w.writeheader()
                w.writerows(rows)
            with open(os.path.join(d, "ledgers.csv"), "w", newline="") as f:
                w = csv.DictWriter(f, ["ledger_index", "close_time_iso", "txn_count"])
                w.writeheader()
                w.writerows(ledgers)
            out = os.path.join(d, "report")
            self.assertEqual(main(["--data", d, "--out", out]), 0)
            report = open(os.path.join(out, "REPORT.md")).read()
            self.assertIn("| Payment | 60 | 60.0% |", report)
            self.assertIn("| 50 | 90 | 99 | 100 | 1 |", report)
            self.assertIn("Gini of tx count per sender: **0.668**", report)
            for c in ("types_by_hour.png", "fees.png", "lorenz.png"):
                self.assertGreater(os.path.getsize(os.path.join(out, "charts", c)), 1000)


if __name__ == "__main__":
    unittest.main()
