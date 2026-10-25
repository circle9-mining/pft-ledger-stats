import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "vlatency"))

from collect_vlatency import Handler, VlState, drop_torn_tail, send  # noqa: E402


def VM(k, li, lh="H", st=100, full=True):
    return {
        "type": "validationReceived",
        "validation_public_key": k,
        "ledger_index": str(li),
        "ledger_hash": f"{lh}{li}",
        "signing_time": st,
        "full": full,
        "validated_hash": f"V{li - 1}",
        "master_key": f"M{k}",
    }


def LC(li, ct=1000):
    return {
        "type": "ledgerClosed",
        "ledger_index": li,
        "ledger_hash": f"H{li}",
        "ledger_time": ct,
        "txn_count": 0,
    }


def handler(state=None):
    out = []
    return Handler(state or VlState(), out.append), out


class TestCollector(unittest.TestCase):
    def test_validation_fields(self):
        h, out = handler()
        h.on_message(VM("n9A", 10, st=846256215), 1.23456)
        self.assertEqual(
            out,
            [
                {
                    "ev": "val",
                    "k": "n9A",
                    "li": 10,
                    "lh": "H10",
                    "st": 846256215,
                    "t": 1.235,
                    "mk": "Mn9A",
                    "vh": "V9",
                    "full": True,
                }
            ],
        )

    def test_duplicate_validation_dropped_but_other_hash_kept(self):
        h, out = handler()
        h.on_message(VM("n9A", 10), 1)
        h.on_message(VM("n9A", 10), 2)
        h.on_message(VM("n9A", 10, lh="X"), 3)  # same key+index, different hash: keep
        self.assertEqual([e["lh"] for e in out], ["H10", "X10"])
        self.assertEqual(h.dups, 1)

    def test_bad_validation_ignored(self):
        h, out = handler()
        h.on_message({"type": "validationReceived", "ledger_index": "x", "ledger_hash": "H"}, 1)
        h.on_message({"type": "validationReceived", "ledger_index": "5"}, 1)
        self.assertEqual(out, [])

    def test_gap_reasons(self):
        h, out = handler()
        h.on_message(LC(1), 1)
        h.on_message(LC(4), 2)
        h.on_connect(reconnect=True)
        h.on_message(LC(7), 3)
        h.on_message(LC(7), 4)  # replay: ignored
        gaps = [(e["from"], e["to"], e["count"], e["reason"]) for e in out if e["ev"] == "gap"]
        self.assertEqual(gaps, [(2, 3, 2, "stream_skip"), (5, 6, 2, "reconnect")])
        self.assertEqual([e["li"] for e in out if e["ev"] == "ledger"], [1, 4, 7])

    def test_resume_dedupe_and_restart_gap(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "vl.jsonl")
            h, out = handler()
            for m, t in [(LC(1), 1), (VM("n9A", 2), 2), (VM("n9B", 2), 2.1)]:
                h.on_message(m, t)
            with open(p, "w") as fh:
                fh.writelines(json.dumps(e) + "\n" for e in out)
            s = VlState().load(p)
            self.assertEqual((s.last_ledger, s.vals, s.last_t), (1, 2, 2.1))
            h2, out2 = handler(s)
            h2.on_message(VM("n9A", 2), 5)  # seen before the restart
            h2.on_message(VM("n9C", 2), 5)
            h2.on_message(LC(4), 6)
            self.assertEqual([e.get("k") for e in out2 if e["ev"] == "val"], ["n9C"])
            gap = [e for e in out2 if e["ev"] == "gap"][0]
            self.assertEqual((gap["from"], gap["to"], gap["reason"]), (2, 3, "restart"))

    def test_torn_tail_repair(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "vl.jsonl")
            good = b'{"ev":"ledger","li":1,"t":1}\n' * 5000  # > one 64 KiB scan block
            with open(p, "wb") as fh:
                fh.write(good + b'{"ev":"val","k":"n9')
            self.assertEqual(drop_torn_tail(p), len(b'{"ev":"val","k":"n9'))
            with open(p, "rb") as fh:
                self.assertEqual(fh.read(), good)
            self.assertEqual(drop_torn_tail(p), 0)

    def test_torn_tail_without_any_newline(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "vl.jsonl")
            with open(p, "wb") as fh:
                fh.write(b'{"ev":"le')
            self.assertEqual(drop_torn_tail(p), 9)
            self.assertEqual(os.path.getsize(p), 0)

    def test_bad_lines_counted_on_load(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "vl.jsonl")
            with open(p, "w") as fh:
                fh.write('{"ev":"ledger","li":3,"t":1}\nnot json\n')
            s = VlState().load(p)
            self.assertEqual((s.last_ledger, s.bad_lines), (3, 1))

    def test_prune_keeps_recent(self):
        s = VlState(keep=10)
        for li in range(1, 40):
            s.apply({"ev": "val", "k": "n9A", "li": li, "lh": f"H{li}", "t": li})
        self.assertFalse(s.has(1, "n9A", "H1"))
        self.assertTrue(s.has(39, "n9A", "H39"))

    def test_only_subscribe_allowed(self):
        with self.assertRaises(ValueError):
            send(None, "submit", tx_blob="00")


if __name__ == "__main__":
    unittest.main()
