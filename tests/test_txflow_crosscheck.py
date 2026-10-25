import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "txflow"))

from crosscheck_flow import main  # noqa: E402


class Crosscheck(unittest.TestCase):
    def run_with(self, public):
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, "flow.jsonl")
            with open(f, "w") as fh:
                for i in range(5):
                    fh.write(
                        json.dumps(
                            {
                                "ev": "validated",
                                "hash": f"H{i}",
                                "ledger_index": 100 + i,
                                "result": "tesSUCCESS",
                            }
                        )
                        + "\n"
                    )
            return main([f, "--count", "3"], fetch=lambda url, h: public(h), sleep=lambda s: None)

    def test_all_match(self):
        def ok(h):
            return {
                "validated": True,
                "ledger_index": 100 + int(h[1]),
                "meta": {"TransactionResult": "tesSUCCESS"},
            }

        self.assertEqual(self.run_with(ok), 0)

    def test_wrong_ledger_or_unvalidated_or_error_fails(self):
        for bad in (
            lambda h: {"validated": True, "ledger_index": 1, "meta": {"TransactionResult": "tesSUCCESS"}},
            lambda h: {
                "validated": False,
                "ledger_index": 100 + int(h[1]),
                "meta": {"TransactionResult": "tesSUCCESS"},
            },
            lambda h: {"error": "txnNotFound"},
        ):
            self.assertEqual(self.run_with(bad), 1)

    def test_network_error_is_mismatch(self):
        def boom(h):
            raise OSError("down")

        self.assertEqual(self.run_with(boom), 1)


if __name__ == "__main__":
    unittest.main()
