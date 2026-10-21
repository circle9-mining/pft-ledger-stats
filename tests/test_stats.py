import unittest

from pftstats import stats


def row(i, t, tx=0, coins=1000):
    return {"ledger_index": i, "ledger_hash": f"H{i}", "close_time": t,
            "close_time_iso": f"2026-10-21T{(t // 3600) % 24:02d}:00:00Z",
            "close_time_resolution": 10, "txn_count": tx, "total_coins": coins}


class TestPercentile(unittest.TestCase):
    def test_matches_linear_interpolation(self):
        v = [1, 2, 3, 4]
        self.assertEqual(stats.percentile(v, 0), 1)
        self.assertEqual(stats.percentile(v, 100), 4)
        self.assertAlmostEqual(stats.percentile(v, 50), 2.5)
        self.assertAlmostEqual(stats.percentile(v, 95), 3.85)

    def test_unsorted_and_single(self):
        self.assertEqual(stats.percentile([5, 1, 3], 50), 3)
        self.assertEqual(stats.percentile([7], 99), 7)

    def test_errors(self):
        with self.assertRaises(ValueError):
            stats.percentile([], 50)
        with self.assertRaises(ValueError):
            stats.percentile([1], 101)

    def test_summarize(self):
        s = stats.summarize([3, 1, 2])
        self.assertEqual((s["min"], s["median"], s["max"], s["count"]), (1, 2, 3, 3))
        self.assertAlmostEqual(s["mean"], 2.0)


class TestIntervals(unittest.TestCase):
    def setUp(self):
        self.rows = [row(1, 0), row(2, 4), row(3, 7), row(5, 30), row(6, 34)]

    def test_gaps(self):
        self.assertEqual(stats.find_gaps(self.rows), [(3, 5)])

    def test_intervals_skip_gap(self):
        iv = stats.close_intervals(self.rows)
        self.assertEqual([i[0] for i in iv], [2, 3, 6])
        self.assertEqual([i[2] for i in iv], [4, 3, 4])

    def test_stalls(self):
        rows = [row(i, i * 4) for i in range(1, 11)] + [row(11, 40 + 20)]
        limit, st = stats.stalls(stats.close_intervals(rows))
        self.assertEqual(limit, 12)
        self.assertEqual([(s[0], s[2]) for s in st], [(11, 20)])

    def test_stalls_floor(self):
        # rounded close times: median delta 1 s, real hiccup of 20 s, 10 s resolution
        rows = [row(1, 0), row(2, 1), row(3, 2), row(4, 10), row(5, 11), row(6, 31)]
        iv = stats.close_intervals(rows)
        lit_limit, lit = stats.stalls(iv)
        self.assertEqual((lit_limit, [s[2] for s in lit]), (3, [8, 20]))
        limit, st = stats.stalls(iv, floor=10)
        self.assertEqual((limit, st), (30, []))
        limit, st = stats.stalls(iv, floor=5)
        self.assertEqual((limit, [s[2] for s in st]), (15, [20]))

    def test_stalls_empty(self):
        self.assertEqual(stats.stalls([]), (0, []))


class TestHourlyAndBurn(unittest.TestCase):
    def test_by_hour_and_burn(self):
        rows = [row(1, 3590, tx=1, coins=1000), row(2, 3600, tx=2, coins=990),
                row(3, 3610, tx=3, coins=985), row(5, 3620, tx=4, coins=900)]
        h = stats.by_hour(rows)
        self.assertEqual(list(h), ["2026-10-21T00", "2026-10-21T01"])
        self.assertEqual(h["2026-10-21T00"], {"ledgers": 1, "txns": 1, "burn_drops": 0})
        # burn across the 3->5 gap is not attributed to an hour
        self.assertEqual(h["2026-10-21T01"], {"ledgers": 3, "txns": 9, "burn_drops": 15})
        total, per = stats.fee_burn(rows)
        self.assertEqual(total, 100)
        self.assertEqual(per, [10, 5])

    def test_quorum_margin(self):
        s = [{"sampled_at": "a", "proposers": 25, "validation_quorum": 17},
             {"sampled_at": "b", "proposers": 16, "validation_quorum": 17},
             {"sampled_at": "c", "proposers": None, "validation_quorum": 17}]
        self.assertEqual([m[3] for m in stats.quorum_margin(s)], [8, -1])


if __name__ == "__main__":
    unittest.main()
