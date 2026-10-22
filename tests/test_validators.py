import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "validators"))

from analyze_validations import analyze, longest_streak  # noqa: E402

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

    def test_longest_streak(self):
        self.assertEqual(longest_streak([1, 2, 3, 4, 5], {1, 5}), 3)


if __name__ == "__main__":
    unittest.main()
