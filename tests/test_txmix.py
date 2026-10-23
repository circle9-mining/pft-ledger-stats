import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "txmix"))

import collect_tx  # noqa: E402
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


class FakeRpc:
    """Serves server_info/ledger for ledgers 100..; stops the collector after ledger `stop_at`."""

    def __init__(self, stop_at):
        self.stop_at = stop_at
        self.count = 0

    def __call__(self, *_args):
        return self

    def call(self, method, params=None):
        self.count += 1
        if method == "server_info":
            return {"info": {"validated_ledger": {"seq": 200}, "server_state": "full"}}
        i = params["ledger_index"]
        if i >= self.stop_at:
            collect_tx._stop = True
        txs = [{"hash": f"T{i}-{k}", "TransactionType": "Payment", "Fee": "10", "Account": f"r{k}",
                "metaData": {"TransactionIndex": k, "TransactionResult": "tesSUCCESS"}} for k in range(2)]
        return {"ledger": {"ledger_index": str(i), "ledger_hash": f"H{i}", "close_time": 1000 + i,
                           "transactions": txs}}


class TestResume(unittest.TestCase):
    """Interrupt/resume of collect_tx.py on the same --out directory (transport-free)."""

    def run_collector(self, out, stop_at, start=None):
        collect_tx._stop = False
        orig = collect_tx.Rpc
        collect_tx.Rpc = FakeRpc(stop_at)
        try:
            argv = ["--out", out, "--log", os.path.join(out, "collect.log"), "--hours", "1"]
            self.assertEqual(collect_tx.main(argv + (["--start", str(start)] if start else [])), 0)
        finally:
            collect_tx.Rpc = orig
            collect_tx._stop = False

    @staticmethod
    def lines(out, name):
        with open(os.path.join(out, name)) as f:
            return f.read().splitlines()

    def test_resume_after_kill_between_appends_and_torn_rows(self):
        with tempfile.TemporaryDirectory() as out:
            self.run_collector(out, stop_at=102, start=100)  # ledgers 100-102, 2 tx each
            before_tx, before_led = self.lines(out, "tx.csv"), self.lines(out, "ledgers.csv")
            self.assertEqual((len(before_tx), len(before_led)), (7, 4))
            # killed after ledger 103's tx rows were appended (one torn) but before its ledgers.csv row,
            # and with a torn ledgers.csv tail
            with open(os.path.join(out, "tx.csv"), "a") as f:
                f.write("103,1103,x,0,T103-0,Payment,tesSUCCESS,10,aa,n,0,none,0\n103,1103,x,1,T10")
            with open(os.path.join(out, "ledgers.csv"), "a") as f:
                f.write("103,H1")
            self.run_collector(out, stop_at=104)
            tx, led = self.lines(out, "tx.csv"), self.lines(out, "ledgers.csv")
            self.assertEqual(tx[:7], before_tx)  # nothing lost or rewritten
            self.assertEqual(led[:4], before_led)
            hashes = [r.split(",")[4] for r in tx[1:]]
            self.assertEqual(hashes, [f"T{i}-{k}" for i in range(100, 105) for k in range(2)])  # no dupes
            self.assertEqual([int(r.split(",")[0]) for r in led[1:]], list(range(100, 105)))  # contiguous
            log = "\n".join(self.lines(out, "collect.log"))
            self.assertIn("RESUME dropped torn last line of tx.csv (16 bytes)", log)
            self.assertIn("RESUME dropped torn last line of ledgers.csv (6 bytes)", log)
            self.assertIn("RESUME dropped 1 tx.csv rows after ledger 102", log)
            self.assertIn("RESUME at ledger 103", log)
            self.assertEqual(log.count("WINDOW START"), 1)

    def test_restart_before_first_ledger_keeps_window(self):
        with tempfile.TemporaryDirectory() as out:
            self.run_collector(out, stop_at=100, start=100)
            os.remove(os.path.join(out, "ledgers.csv"))  # killed before the first ledgers.csv row
            state = open(os.path.join(out, "state.json")).read()
            self.run_collector(out, stop_at=101)
            self.assertEqual(open(os.path.join(out, "state.json")).read(), state)
            hashes = [r.split(",")[4] for r in self.lines(out, "tx.csv")[1:]]
            self.assertEqual(hashes, ["T100-0", "T100-1", "T101-0", "T101-1"])


class TestCrosscheckSkip(unittest.TestCase):
    """A ledger listed in complete_ledgers but answered with lgrNotFound is skipped, not a mismatch."""

    def test_unserved_ledger_skipped(self):
        import contextlib
        import io

        import crosscheck_tx

        class Rpc:
            def __init__(self, *_args):
                pass

            def call(self, method, params=None):
                if method == "server_info":
                    return {"info": {"complete_ledgers": "100-110"}}
                if params["ledger_index"] == 100:
                    raise LookupError("lgrNotFound")
                i = params["ledger_index"]
                return {"ledger": {"ledger_hash": f"H{i}", "transactions": [{"hash": f"T{i}"}]}}

        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "ledgers.csv"), "w") as f:
                f.write("ledger_index,ledger_hash,txn_count\n")
                f.writelines(f"{i},H{i},1\n" for i in range(100, 103))
            with open(os.path.join(d, "tx.csv"), "w") as f:
                f.write("ledger_index,tx_hash\n")
                f.writelines(f"{i},T{i}\n" for i in range(100, 103))
            orig, crosscheck_tx.Rpc = crosscheck_tx.Rpc, Rpc
            try:
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    rc2 = crosscheck_tx.main(["--data", d, "--count", "2", "--seed", "1"])
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    rc3 = crosscheck_tx.main(["--data", d, "--count", "3", "--seed", "1"])
            finally:
                crosscheck_tx.Rpc = orig
            self.assertEqual(rc2, 0)
            self.assertIn("CROSSCHECK PASS: 2/2", out.getvalue())
            self.assertEqual(rc3, 2)  # only 2 of 3 can be fetched -> "too few", never a false PASS


if __name__ == "__main__":
    unittest.main()
