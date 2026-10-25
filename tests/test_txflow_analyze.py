import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "txflow"))

from analyze_flow import analyze, build, classify, fee_periods, main  # noqa: E402


def L(i, t=None):
    return {"ev": "ledger", "ledger_index": i, "t": float(t if t is not None else i)}


def P(h, cur, t, er="tesSUCCESS", typ="Payment"):
    return {
        "ev": "proposed",
        "hash": h,
        "t": t,
        "ledger_current_index": cur,
        "engine_result": er,
        "tx_type": typ,
    }


def V(h, idx, t, typ="Payment"):
    return {"ev": "validated", "hash": h, "t": t, "ledger_index": idx, "result": "tesSUCCESS", "tx_type": typ}


def F(i, open_fee=10, q=0):
    return {
        "ev": "fee",
        "ledger_index": i,
        "t": float(i),
        "queue": q,
        "open_ledger_fee": open_fee,
        "base_fee": 10,
    }


class Latency(unittest.TestCase):
    def test_seconds_and_ledgers(self):
        evs = [P("A", 100, 10.0), P("B", 100, 11.0, typ="OfferCreate"), L(100), L(101)]
        evs += [
            V("A", 100, 13.5),
            V("B", 101, 15.0, typ="OfferCreate"),
            P("A", 101, 12.0),
        ]  # later re-sighting ignored
        s = analyze(evs, n=5)
        self.assertEqual(s["latency_s"]["count"], 2)
        self.assertAlmostEqual(s["latency_s"]["max"], 4.0)
        self.assertAlmostEqual(s["latency_s"]["p50"], 3.75)  # 3.5 and 4.0
        self.assertEqual(s["latency_l_counts"], {0: 1, 1: 1})
        self.assertEqual(set(s["latency_s_by_type"]), {"Payment", "OfferCreate"})

    def test_validated_without_proposed_counted_not_in_latency(self):
        s = analyze([L(100), V("X", 100, 5.0)], n=5)
        self.assertEqual((s["validated_without_proposed"], s["latency_s"]), (1, None))


class Outcomes(unittest.TestCase):
    def test_five_classes(self):
        evs = [L(i) for i in range(100, 131) if i not in (121, 122)]
        evs.append({"ev": "gap", "from": 121, "to": 122, "count": 2, "reason": "restart"})
        evs += [
            P("ok", 100, 1.0),
            V("ok", 102, 3.0),
            P("late", 100, 1.0),
            V("late", 106, 9.0),
            P("never", 101, 1.0, er="telCAN_NOT_QUEUE_FEE"),
            P("gap", 118, 1.0, er="terQUEUED"),  # 118..123 overlaps the gap
            P("end", 127, 1.0),  # needs ledger 132, data ends at 130
        ]
        oc = classify(build(evs), 5)
        self.assertEqual(
            oc,
            {"ok": "validated", "late": "late", "never": "never", "gap": "unknown_gap", "end": "pending_end"},
        )
        s = analyze(evs, 5)
        self.assertEqual(s["never_share"]["tel"], 1.0)
        self.assertEqual(s["never_share"]["tes"], 0.0)  # validated + late, no never
        self.assertIsNone(s["never_share"]["ter"])  # only unknown_gap: no denominator
        self.assertEqual(s["never_hashes"], ["never"])

    def test_boundary_exactly_n(self):
        evs = [L(i) for i in range(100, 111)] + [
            P("a", 100, 1.0),
            V("a", 105, 2.0),
            P("b", 100, 1.0),
            V("b", 106, 2.0),
        ]
        self.assertEqual(classify(build(evs), 5), {"a": "validated", "b": "late"})


class Fees(unittest.TestCase):
    def test_escalation_periods(self):
        fees = [F(1), F(2, 300, 4), F(3, 500, 9), F(4), F(5, 20, 1), F(7, 20, 1)]  # 6 missing -> new period
        p = fee_periods(fees)
        self.assertEqual(
            [(x["start"], x["end"], x["max_open_ledger_fee"], x["max_queue"]) for x in p],
            [(2, 3, 500, 9), (5, 5, 20, 1), (7, 7, 20, 1)],
        )

    def test_report_files(self):
        import json

        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, "flow.jsonl")
            with open(f, "w") as fh:
                for e in [L(1), F(1, 300, 2), P("a", 1, 1.0), V("a", 2, 4.0), L(2), "torn"]:
                    fh.write((json.dumps(e) if isinstance(e, dict) else e) + "\n")
            self.assertEqual(main([f, "--report", d, "--n-ledgers", "1"]), 0)
            s = json.load(open(os.path.join(d, "summary.json")))
            self.assertEqual(
                (s["bad_lines"], s["escalated_samples"], s["outcomes"]), (1, 1, {"validated": 1})
            )
            self.assertIn("| all, seconds | 1 | 3.00 |", open(os.path.join(d, "REPORT.md")).read())


if __name__ == "__main__":
    unittest.main()
