import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "txflow"))

from crosscheck_flow import main  # noqa: E402


class Crosscheck(unittest.TestCase):
    def run_with(self, public, tries=None):
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
            extra = ["--tries", str(tries)] if tries else []
            return main([f, "--count", "3", *extra], fetch=lambda url, h: public(h), sleep=lambda s: None)

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

    def test_transient_error_is_retried_then_matches(self):
        calls = {}

        def flaky(h):
            calls[h] = calls.get(h, 0) + 1
            if calls[h] == 1:
                return {"error": "lgrNotFound"}
            return {
                "validated": True,
                "ledger_index": 100 + int(h[1]),
                "meta": {"TransactionResult": "tesSUCCESS"},
            }

        self.assertEqual(self.run_with(flaky), 0)
        self.assertEqual(sorted(calls.values()), [2, 2, 2])

    def test_persistent_mismatch_fails_after_all_tries(self):
        calls = []

        def bad(h):
            calls.append(h)
            return {"validated": True, "ledger_index": 1, "meta": {"TransactionResult": "tesSUCCESS"}}

        self.assertEqual(self.run_with(bad, tries=3), 1)
        self.assertEqual(len(calls), 9)


if __name__ == "__main__":
    unittest.main()
