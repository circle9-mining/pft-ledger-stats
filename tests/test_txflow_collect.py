import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "txflow"))

from collect_flow import FlowState, Handler, Writer, drop_torn_tail  # noqa: E402


def ledger(i):
    return {
        "type": "ledgerClosed",
        "ledger_index": i,
        "ledger_hash": f"L{i}",
        "ledger_time": i,
        "txn_count": 0,
    }


def prop(h, cur, t="Payment", er="tesSUCCESS"):
    return {
        "type": "transaction",
        "validated": False,
        "engine_result": er,
        "ledger_current_index": cur,
        "transaction": {"hash": h, "TransactionType": t, "Fee": "12"},
    }


def val(h, idx, t="Payment", res="tesSUCCESS"):
    return {
        "type": "transaction",
        "validated": True,
        "engine_result": res,
        "ledger_index": idx,
        "meta": {"TransactionResult": res},
        "transaction": {"hash": h, "TransactionType": t, "Fee": "12"},
    }


class Collect(unittest.TestCase):
    def setUp(self):
        self.out = []
        self.h = Handler(FlowState(), self.out.append)

    def evs(self, kind):
        return [e for e in self.out if e["ev"] == kind]

    def test_proposed_validated_dedupe(self):
        self.h.on_message(prop("A", 11), 100.0)
        self.h.on_message(prop("A", 11), 100.5)  # same hash again from the other stream: first-seen kept
        self.assertEqual(self.h.on_message(ledger(11), 103.0), 11)
        self.h.on_message(val("A", 11), 103.1)
        self.h.on_message(val("A", 11), 103.2)  # validated tx arrives on both streams
        self.assertEqual([e["t"] for e in self.evs("proposed")], [100.0])
        self.assertEqual(len(self.evs("validated")), 1)
        v = self.evs("validated")[0]
        self.assertEqual(
            (v["ledger_index"], v["result"], v["fee"], v["tx_type"]), (11, "tesSUCCESS", 12, "Payment")
        )

    def test_gap_stream_skip(self):
        for i in (10, 11, 14):
            self.h.on_message(ledger(i), float(i))
        g = self.evs("gap")
        self.assertEqual(len(g), 1)
        self.assertEqual(
            (g[0]["from"], g[0]["to"], g[0]["count"], g[0]["reason"]), (12, 13, 2, "stream_skip")
        )

    def test_gap_reconnect_label_once(self):
        self.h.on_message(ledger(10), 1.0)
        self.h.on_connect(reconnect=True)
        self.h.on_message(ledger(13), 2.0)
        self.h.on_message(ledger(15), 3.0)
        self.assertEqual(
            [(g["from"], g["reason"]) for g in self.evs("gap")], [(11, "reconnect"), (14, "stream_skip")]
        )

    def test_reconnect_without_missed_ledger_no_gap(self):
        self.h.on_message(ledger(10), 1.0)
        self.h.on_connect(reconnect=True)
        self.h.on_message(ledger(11), 2.0)
        self.h.on_message(ledger(13), 3.0)
        self.assertEqual([g["reason"] for g in self.evs("gap")], ["stream_skip"])

    def test_fee_event(self):
        r = {
            "current_ledger_size": "3",
            "current_queue_size": "2",
            "expected_ledger_size": "32",
            "max_queue_size": "2000",
            "drops": {"base_fee": "10", "median_fee": "5000", "minimum_fee": "10", "open_ledger_fee": "256"},
        }
        self.h.on_fee(11, r, 5.0)
        f = self.evs("fee")[0]
        self.assertEqual(
            (f["ledger_index"], f["queue"], f["open_ledger_fee"], f["base_fee"]), (11, 2, 256, 10)
        )

    def test_no_hash_ignored(self):
        self.h.on_message({"type": "transaction", "validated": False, "transaction": {}}, 1.0)
        self.assertEqual(self.out, [])


class Resume(unittest.TestCase):
    def test_torn_tail_and_restart_gap(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "flow.jsonl")
            w = Writer(p)
            h = Handler(FlowState(), w)
            h.on_message(prop("A", 11), 1.0)
            h.on_message(ledger(10), 2.0)
            h.on_message(ledger(11), 3.0)
            h.on_message(val("A", 11), 3.1)
            w.fh.close()
            with open(p, "a") as fh:
                fh.write('{"ev":"proposed","hash":"B","t":4.0,"tx_t')  # killed mid-write
            size = os.path.getsize(p)
            self.assertEqual(drop_torn_tail(p), len('{"ev":"proposed","hash":"B","t":4.0,"tx_t'))
            self.assertEqual(os.path.getsize(p), size - 41)
            self.assertEqual(drop_torn_tail(p), 0)
            st = FlowState().load(p)
            self.assertEqual((st.last_ledger, st.proposed, st.validated, st.bad_lines), (11, {"A"}, {"A"}, 0))
            out = []
            h2 = Handler(st, out.append)
            h2.on_message(ledger(11), 9.0)  # overlap after restart: not written twice
            h2.on_message(val("A", 11), 9.1)  # already recorded
            h2.on_message(prop("A", 12), 9.2)  # already seen proposed
            h2.on_message(ledger(15), 10.0)
            self.assertEqual([e["ev"] for e in out], ["gap", "ledger"])
            self.assertEqual((out[0]["from"], out[0]["to"], out[0]["reason"]), (12, 14, "restart"))

    def test_bad_middle_line_counted(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "flow.jsonl")
            with open(p, "w") as fh:
                fh.write(json.dumps({"ev": "ledger", "ledger_index": 5}) + "\nnot json\n")
            st = FlowState().load(p)
            self.assertEqual((st.last_ledger, st.bad_lines), (5, 1))


if __name__ == "__main__":
    unittest.main()


class DrillChecker(unittest.TestCase):
    """resume_check.py against synthetic captures shaped like a real drill (positive + negatives)."""

    def build(self, d, dup=False, torn=b'{"ev":"led'):
        import resume_check  # noqa: F401

        p = os.path.join(d, "final.jsonl")

        def line(e):
            return (json.dumps(e, separators=(",", ":")) + "\n").encode()

        def sess(resumed, torn_bytes=0):
            return line({"ev": "session", "resumed": resumed, "torn_bytes": torn_bytes, "window_end": 99})

        part1 = sess(False) + b"".join(line({"ev": "ledger", "ledger_index": i}) for i in (10, 11))
        part1 += line({"ev": "proposed", "hash": "A"}) + line({"ev": "stop", "reason": "SIGTERM"})
        part2 = sess(True) + line({"ev": "gap", "from": 12, "to": 13, "count": 2, "reason": "restart"})
        part2 += line({"ev": "ledger", "ledger_index": 14})
        part3 = sess(True, len(torn)) + line(
            {"ev": "gap", "from": 15, "to": 15, "count": 1, "reason": "restart"}
        )
        part3 += line({"ev": "ledger", "ledger_index": 16}) + line({"ev": "validated", "hash": "A"})
        if dup:
            part3 += line({"ev": "proposed", "hash": "A"})
        part3 += line({"ev": "stop", "reason": "window complete"})
        for name, body in (("term", part1), ("kill", part1 + part2 + torn), ("final", part1 + part2 + part3)):
            with open(os.path.join(d, name + ".jsonl"), "wb") as fh:
                fh.write(body)
        return p, os.path.join(d, "term.jsonl"), os.path.join(d, "kill.jsonl")

    def test_positive_and_negatives(self):
        from resume_check import main

        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(main(*self.build(d)), 0)
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(main(*self.build(d, dup=True)), 1)
        with tempfile.TemporaryDirectory() as d:
            f, t, k = self.build(d)
            with open(t, "ab") as fh:
                fh.write(b"rewritten\n")  # snapshot no longer a prefix
            self.assertEqual(main(f, t, k), 1)
