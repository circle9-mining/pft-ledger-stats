"""Tests for govern/crosscheck_govern.py with a stub fetch (no network). Votes are SYNTHETIC."""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "govern"))

from crosscheck_govern import main  # noqa: E402

X, Y = "AA" * 32, "BB" * 32
KEYS = ["A", "B", "C", "D", "E"]


def capture(yes_by_voting):
    """Ledgers 255..1024 with votes on 255, 511, 767; yes_by_voting[li] = (amendment, number of UNL yes)."""
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "unl-start.json"), "w") as fh:
        json.dump({"trusted_validator_keys": KEYS, "signing_keys": {k: "s" + k for k in KEYS}}, fh)
    with open(os.path.join(d, "govern.jsonl"), "w") as fh:
        for li in range(255, 1025):
            fh.write(json.dumps({"ev": "ledger", "li": li, "lh": f"H{li}"}) + "\n")
            if (li + 1) % 256 == 0:
                a, n = yes_by_voting.get(li, (None, 0))
                for i, k in enumerate(KEYS):
                    am = [a] if a and i < n else []
                    fh.write(
                        json.dumps({"ev": "vote", "k": "s" + k, "li": li, "lh": f"H{li}", "am": am}) + "\n"
                    )
    return d


def node(majs=(), enabled=("E1",)):
    n = {"Amendments": list(enabled)}
    if majs:
        n["Majorities"] = [{"Majority": {"Amendment": a, "CloseTime": 1}} for a in majs]
    return {"status": "success", "validated": True, "node": n}


def run(d, answers):
    calls = []

    def fetch(url, li):
        calls.append(li)
        r = answers[li]
        return r.pop(0) if isinstance(r, list) else r

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = main([d, "--count", "20"], fetch=fetch, sleep=lambda s: None)
    return rc, out.getvalue(), calls


class TestCrosscheck(unittest.TestCase):
    def test_no_votes_matches_empty_majorities(self):
        rc, out, calls = run(capture({}), {512: node(), 768: node(), 1024: node()})
        self.assertEqual((rc, calls), (0, [512, 768, 1024]))  # flag 256, 512, 768 -> next flag
        self.assertIn("RESULT: 3/3 match", out)

    def test_majority_recomputed_and_found(self):
        d = capture({511: (X, 4), 767: (X, 3)})  # 80% on 511 -> majority at 768; 60% on 767 -> lost at 1024
        rc, out, _ = run(d, {512: node(), 768: node([X]), 1024: node()})
        self.assertEqual(rc, 0, out)
        self.assertIn(
            f"MATCH flag 512: voting ledger 511 UNL validations 5/5, recomputed majorities ['{X}']", out
        )

    def test_missing_majority_is_mismatch(self):
        d = capture({511: (X, 4)})
        rc, out, _ = run(d, {512: node(), 768: node(), 1024: node()})
        self.assertEqual(rc, 1)
        self.assertIn("MISMATCH flag 512", out)
        self.assertIn("RESULT: 2/3 match", out)

    def test_enabled_amendment_not_expected_in_majorities(self):
        d = capture({511: (Y, 5)})
        rc, out, _ = run(d, {512: node(), 768: node(enabled=("E1", Y)), 1024: node()})
        self.assertEqual(rc, 0, out)

    def test_transient_error_retried_and_printed(self):
        err = {"status": "error", "error": "lgrNotFound"}
        rc, out, calls = run(capture({}), {512: [err, err, node()], 768: node(), 1024: node()})
        self.assertEqual(rc, 0)
        self.assertEqual(out.count("RETRY"), 2)
        self.assertIn("RETRY 1/3 flag 256 next 512: error=lgrNotFound -> again in 2s", out)
        self.assertEqual(calls, [512, 512, 512, 768, 1024])

    def test_persistent_error_fails(self):
        err = {"status": "error", "error": "internal"}
        rc, out, _ = run(capture({}), {512: err, 768: node(), 1024: node()})
        self.assertEqual(rc, 1)
        self.assertEqual(out.count("RETRY"), 3)
        self.assertIn("MISMATCH flag 256", out)

    def test_unvalidated_answer_not_trusted(self):
        rc, out, _ = run(capture({}), {512: dict(node(), validated=False), 768: node(), 1024: node()})
        self.assertEqual(rc, 1)
        self.assertIn("error=incomplete answer", out)


if __name__ == "__main__":
    unittest.main()
