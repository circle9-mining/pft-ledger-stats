import contextlib
import io
import json
import os
import random
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "vlatency"))

import crosscheck_vlatency as xc  # noqa: E402
from analyze_vlatency import (  # noqa: E402
    RIPPLE_EPOCH,
    analyze,
    build,
    eff_close_time,
    render,
    ripple_to_unix,
    round_close_time,
)

T0 = 1792941200.0  # unix receive time of ledger 100's ledgerClosed


def led(li, ct, t, lh=None):
    return {"ev": "ledger", "li": li, "lh": lh or f"H{li}", "ct": ct, "txn": 0, "t": t}


def val(k, li, t, st, lh=None, full=True):
    return {"ev": "val", "k": k, "li": li, "lh": lh or f"H{li}", "st": st, "t": t, "full": full}


def fixture():
    """3 ledgers, 3 validators. A: always first; B: 0.5 s behind; C: 2.5 s behind on ledger 101 (late)."""
    evs = []
    for i, li in enumerate((100, 101, 102)):
        tc = T0 + 3 * i
        st = int(tc - RIPPLE_EPOCH) - 1  # signed ~1 s before the ledgerClosed this node saw
        evs.append(val("kA", li, tc - 0.8, st))
        evs.append(val("kB", li, tc - 0.3, st))
        evs.append(val("kC", li, tc - 0.8 + (2.5 if li == 101 else 0.2), st))
        evs.append(led(li, 846256400 + i, tc))
    return evs


class TestTime(unittest.TestCase):
    def test_epoch_conversion(self):
        self.assertEqual(RIPPLE_EPOCH, 946684800)
        self.assertEqual(ripple_to_unix(0), 946684800)
        # 846256400 Ripple = 2026-10-25T15:13:20Z (ledger 7473744 in the live data)
        self.assertEqual(ripple_to_unix(846256400), 1792941200)

    def test_round_close_time(self):
        self.assertEqual(round_close_time(846256404, 10), 846256400)
        self.assertEqual(round_close_time(846256405, 10), 846256410)  # half rounds up (rippled)
        self.assertEqual(round_close_time(846256399, 10), 846256400)
        self.assertEqual(round_close_time(846256401, 1), 846256401)
        self.assertEqual(round_close_time(0, 10), 0)

    def test_eff_close_time_bumps_to_prior_plus_one(self):
        self.assertEqual(eff_close_time(846256402, 10, 846256400), 846256401)
        self.assertEqual(eff_close_time(846256406, 10, 846256400), 846256410)

    def test_ct_kinds(self):
        evs = [led(1, 846256400, T0), led(2, 846256401, T0 + 3), led(3, 846256402, T0 + 6),
               led(4, 846256410, T0 + 9), led(6, 846256411, T0 + 15)]
        kinds = {li: L["ct_kind"] for li, L in build(evs)["ledgers"].items()}
        # 6 follows a missing ledger 5, so it is not provably "bumped"
        self.assertEqual(kinds, {1: "rounded", 2: "bumped", 3: "bumped", 4: "rounded", 6: "other"})


class TestAnalyze(unittest.TestCase):
    def test_per_validator_latency_and_late(self):
        s = analyze(fixture(), late=2.0)
        r = {x["key"]: x for x in s["per_validator"]}
        self.assertEqual(r["kA"]["d_first"]["max"], 0.0)
        self.assertAlmostEqual(r["kB"]["d_first"]["p50"], 0.5)
        self.assertAlmostEqual(r["kC"]["d_first"]["max"], 2.5)
        self.assertAlmostEqual(r["kC"]["late_share"], 1 / 3, places=4)
        self.assertEqual(r["kA"]["late_share"], 0)
        self.assertAlmostEqual(r["kA"]["d_closed"]["p50"], -0.8)
        self.assertAlmostEqual(s["network_late_share"], 1 / 9, places=4)
        self.assertEqual([x["key"] for x in s["per_validator"]], ["kA", "kC", "kB"])

    def test_skew_uses_ripple_epoch(self):
        s = analyze(fixture())
        r = {x["key"]: x for x in s["per_validator"]}
        # st = floor(tc) - 1 s in Ripple seconds; A receives at tc - 0.8 -> skew -0.2
        self.assertAlmostEqual(r["kA"]["skew"]["p50"], -0.2, places=3)
        self.assertAlmostEqual(r["kB"]["skew"]["p50"], -0.7, places=3)

    def test_d_ct_only_on_rounded_ledgers(self):
        s = analyze(fixture())
        r = {x["key"]: x for x in s["per_validator"]}
        # only ledger 100 (ct 846256400) is rounded; 101/102 are bumped
        self.assertEqual(s["ct_kinds"], {"rounded": 1, "bumped": 2})
        self.assertEqual(r["kA"]["d_ct"]["n"], 1)
        self.assertAlmostEqual(r["kA"]["d_ct"]["p50"], -0.8)

    def test_out_of_order_events_same_result(self):
        evs = fixture()
        shuffled = evs[:]
        random.Random(7).shuffle(shuffled)
        self.assertNotEqual(shuffled, evs)
        self.assertEqual(analyze(shuffled), analyze(evs))

    def test_resume_duplicates_dropped(self):
        evs = fixture()
        replay = [dict(e, t=e["t"] + 30) for e in evs if e["ev"] == "val" and e["li"] == 102]
        s = analyze(evs + [{"ev": "session", "resumed": True}] + replay)
        self.assertEqual(s["duplicates_dropped"], 3)
        self.assertEqual(s["validations"], 9)
        self.assertEqual(s, dict(analyze(evs), duplicates_dropped=3, sessions=1))

    def test_non_validated_and_unmatched(self):
        evs = fixture() + [val("kB", 101, T0 + 3.1, 1, lh="FORK"), val("kB", 105, T0 + 20, 1)]
        s = analyze(evs)
        r = {x["key"]: x for x in s["per_validator"]}
        self.assertEqual(s["non_validated_validations"], 1)
        self.assertEqual(r["kB"]["non_validated"], 1)
        self.assertEqual(s["unmatched_validations"], 1)
        self.assertEqual(r["kB"]["matched"], 3)

    def test_active_span_coverage_skips_gaps(self):
        evs = fixture() + [led(103, 846256410, T0 + 9), led(105, 846256411, T0 + 15),
                           {"ev": "gap", "from": 104, "to": 104, "count": 1, "reason": "restart"},
                           val("kA", 105, T0 + 14.5, 1)]
        s = analyze(evs)
        r = {x["key"]: x for x in s["per_validator"]}
        self.assertEqual(r["kA"]["span_coverage"], 0.8)  # 100,101,102,105 of observed 100-103,105
        self.assertEqual(r["kB"]["span_coverage"], 1.0)  # span 100-102
        self.assertEqual(r["kB"]["window_coverage"], 0.6)

    def test_partial_counted(self):
        s = analyze(fixture() + [val("kD", 100, T0 - 0.5, 1, full=False)])
        self.assertEqual(s["partial_validations"], 1)

    def test_render_has_sections(self):
        md = render(analyze(fixture()))
        for h in ("## Network", "## Per validator", "## Hourly (network d_first)", "`kC`"):
            self.assertIn(h, md)


class TestCrosscheck(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "vl.jsonl")
        with open(self.p, "w") as fh:
            for i in range(30):
                fh.write(json.dumps(led(1000 + i, 846256400 + i, T0 + 3 * i)) + "\n")

    def fake(self, bad=None, transient=0):
        calls = {"n": 0}

        def fetch(url, method, params):
            if method == "server_info":
                return {"info": {"complete_ledgers": "990-1025"}}
            li = params["ledger_index"]
            if "public" in url and calls["n"] < transient:
                calls["n"] += 1
                return {"error": "lgrNotFound"}
            lh, ct = f"H{li}", 846256400 + li - 1000
            if bad and "public" in url and li == bad[0]:
                lh, ct = bad[1], bad[2]
            lg = {"ledger_hash": lh, "close_time": ct, "close_time_resolution": 10, "close_flags": 0}
            return {"validated": True, "ledger": lg}

        return fetch

    def run_xc(self, fetch, count=20):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = xc.main([self.p, "--count", str(count), "--rpc", "https://public"], fetch=fetch,
                         sleep=lambda s: None)
        return rc, buf.getvalue().splitlines()

    def test_all_match(self):
        rc, out = self.run_xc(self.fake())
        self.assertEqual(rc, 0)
        self.assertIn("RESULT: PASS 20/20 match", out[-1])
        self.assertIn("pool 26 inside local complete_ledgers 990-1025", out[0])  # collected 1000..1025

    def test_hash_mismatch_fails(self):
        rc, out = self.run_xc(self.fake(bad=(1010, "OTHER", 846256410)), count=26)
        self.assertEqual(rc, 1)
        self.assertTrue(any(x.startswith("MISMATCH ledger 1010") and "public hash=OTHER" in x for x in out))

    def test_close_time_mismatch_fails(self):
        rc, out = self.run_xc(self.fake(bad=(1010, "H1010", 846256499)), count=26)
        self.assertEqual(rc, 1)
        self.assertTrue(any(x.startswith("MISMATCH ledger 1010") and "ct=846256499" in x for x in out))

    def test_transient_retried_and_logged(self):
        rc, out = self.run_xc(self.fake(transient=2))
        self.assertEqual(rc, 0)
        self.assertEqual(sum(1 for x in out if x.strip().startswith("retry ")), 2)

    def test_persistent_error_fails(self):
        rc, out = self.run_xc(self.fake(transient=999))
        self.assertEqual(rc, 1)
        self.assertTrue(any("giving up" in x for x in out))


if __name__ == "__main__":
    unittest.main()
