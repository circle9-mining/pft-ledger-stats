import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "validators"))

import json  # noqa: E402
import tempfile  # noqa: E402

from analyze_validations import analyze, churn, longest_streak, render, unl_changes, unl_split  # noqa: E402
from crosscheck_validations import compare  # noqa: E402

A, B, C = "nHvalidatorA", "nHvalidatorB", "nHvalidatorC"


def ledger(i):
    return {"ledger_index": str(i), "ledger_hash": f"H{i}", "ledger_time": str(1000 + 3 * i)}


def val(i, m, h=None, sign_delta=1, lag_ms=250):
    st = 1000 + 3 * i + sign_delta
    sec = st + 946684800
    recv = datetime.fromtimestamp(sec + lag_ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")
    recv = recv[:-3] + "Z"
    return {"ledger_index": str(i), "ledger_hash": h or f"H{i}", "master_key": m,
            "signing_time": str(st), "recv_utc": recv}


class TestParticipation(unittest.TestCase):
    def setUp(self):
        self.ledgers = [ledger(i) for i in range(1, 101)]
        vals = []
        for i in range(1, 101):
            vals.append(val(i, A))
            if not 41 <= i <= 50:          # B misses 10 consecutive ledgers
                vals.append(val(i, B))
            if i <= 50:                    # C leaves after ledger 50, and disagrees on ledger 7
                vals.append(val(i, C, h="X" if i == 7 else None))
        vals.append(val(5, A))             # duplicate validation must not double count
        self.rows, self.per_ledger = analyze(self.ledgers, vals, trusted={A, B}, quorum=2)
        self.by = {r["master_key"]: r for r in self.rows}

    def test_positive_control_10_of_100(self):
        b = self.by[B]
        self.assertEqual(b["participation_pct"], 90.0)
        self.assertEqual(b["missed"], 10)
        self.assertEqual(b["longest_missed_streak"], 10)
        self.assertEqual(b["active_participation_pct"], 90.0)
        print(f"\npositive control: {b['master_key']} participation={b['participation_pct']}% "
              f"missed={b['missed']} longest_streak={b['longest_missed_streak']}")

    def test_full_participation_and_dedupe(self):
        a = self.by[A]
        self.assertEqual(a["canonical"], 100)
        self.assertEqual(a["participation_pct"], 100.0)
        self.assertEqual(a["longest_missed_streak"], 0)

    def test_non_canonical_and_churn(self):
        c = self.by[C]
        self.assertEqual(c["non_canonical"], 1)
        self.assertEqual(c["canonical"], 49)
        self.assertEqual(c["longest_missed_streak"], 50)
        self.assertEqual((c["first_ledger"], c["last_ledger"]), (1, 50))
        self.assertEqual(c["trusted"], 0)

    def test_delays(self):
        self.assertEqual(self.by[A]["median_sign_delay_s"], 1)
        self.assertAlmostEqual(self.by[A]["median_recv_lag_s"], 0.25, places=2)

    def test_quorum_per_ledger(self):
        below = [r["ledger_index"] for r in self.per_ledger if r["below_quorum"]]
        self.assertEqual(below, list(range(41, 51)))
        self.assertEqual(self.per_ledger[0]["all_canonical"], 3)

    def test_unl_split_order(self):
        self.assertEqual([r["trusted"] for r in self.rows], [1, 1, 0])

    def test_trusted_never_seen(self):
        rows, _ = analyze(self.ledgers, [], trusted={"nHsilent"}, quorum=1)
        self.assertEqual(rows[0]["participation_pct"], 0.0)
        self.assertEqual(rows[0]["longest_missed_streak"], 100)

    def test_active_participation_late_joiner(self):
        late = [val(i, "nHlate") for i in range(200, 301)]
        ledgers = [ledger(i) for i in range(1, 301)]
        rows, _ = analyze(ledgers, late, trusted=set())
        r = rows[0]
        self.assertEqual(r["participation_pct"], 33.67)        # 101 of 300 window ledgers
        self.assertEqual((r["active_ledgers"], r["active_participation_pct"]), (101, 100.0))
        self.assertEqual(self.by[C]["active_participation_pct"], 98.0)   # 49 of 50 while active

    def test_churn(self):
        late = [val(i, "nHlate") for i in range(200, 301)]
        ledgers = [ledger(i) for i in range(1, 301)]
        rows, _ = analyze(ledgers, late + [val(i, A) for i in range(1, 301)], trusted={A, "nHsilent"})
        joined, left, silent = churn(rows, 1, 300, margin=100)
        self.assertEqual((joined, left, silent), (["nHlate"], [], ["nHsilent"]))
        joined, left, _ = churn(self.rows, 1, 100, margin=40)
        self.assertEqual((joined, left), ([], [C]))     # C last seen at 50, 50 > 40 before the end

    def test_unl_split(self):
        sp = unl_split(self.rows)
        self.assertEqual(sp["unl"]["validators"], 2)
        self.assertEqual(sp["unl"]["median_participation_pct"], 95.0)    # median(100, 90)
        self.assertEqual(sp["unl"]["min_participation_pct"], 90.0)
        self.assertEqual(sp["non_unl"]["validators"], 1)
        self.assertEqual(sp["non_unl"]["canonical_validations"], 49)

    def test_unl_changes(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            for keys in ([A, B], [A, B], [A, C]):
                d = {"command": "validators", "result": {"trusted_validator_keys": keys}}
                f.write(json.dumps(d) + "\n")
        try:
            first, last = unl_changes(f.name)
        finally:
            os.unlink(f.name)
        self.assertEqual((last - first, first - last), ({C}, {B}))

    def test_report_render(self):
        md = render(self.rows, self.per_ledger, {"start_utc": "S", "end_utc": "E"}, [], 2, {A, B},
                    {"added": [], "removed": []}, [], [C], [], 251, charts=False)
        self.assertIn("Ledgers observed: **100** (1–100)", md)
        self.assertIn("Ledgers below quorum in this node's view: **10**", md)
        self.assertIn(f"| `{B}` | yes | 90.0 | 90.0 | 10 | 10 |", md)

    def test_longest_streak(self):
        self.assertEqual(longest_streak([1, 2, 3, 4, 5], {1, 5}), 3)


class TestCrosscheckCompare(unittest.TestCase):
    ROW = {"ledger_index": "7", "ledger_hash": "AA", "ledger_time": "1021", "txn_count": "2"}

    def test_match(self):
        ok, f = compare(self.ROW, 30, {"ledger_hash": "AA", "close_time": 1021, "transactions": ["t1", "t2"]})
        self.assertTrue(ok)
        self.assertEqual(f["canonical_validations"], 30)

    def test_each_field_mismatch_fails(self):
        good = {"ledger_hash": "AA", "close_time": 1021, "transactions": ["t1", "t2"]}
        for bad in ({**good, "ledger_hash": "BB"}, {**good, "close_time": 1022},
                    {**good, "transactions": ["t1"]}):
            self.assertFalse(compare(self.ROW, 30, bad)[0], bad)
        self.assertFalse(compare(self.ROW, 0, good)[0], "no canonical validation recorded")


if __name__ == "__main__":
    unittest.main()
