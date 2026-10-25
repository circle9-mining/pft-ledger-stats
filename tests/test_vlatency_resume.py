"""Tests for vlatency/vl_resume_check.py on synthetic captures built with the collector's own classes.

Each negative asserts the specific check that must fail (and that the others still pass), and the
positive capture is the same data, so an absence check can never pass on an empty file.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "vlatency"))

from collect_vlatency import Handler, VlState, Writer, drop_torn_tail  # noqa: E402
from vl_resume_check import checks  # noqa: E402

WEND = 2000


def VM(k, li):
    return {
        "type": "validationReceived",
        "validation_public_key": k,
        "ledger_index": str(li),
        "ledger_hash": f"H{li}",
        "signing_time": 900 + li,
        "full": True,
    }


def LC(li):
    return {"type": "ledgerClosed", "ledger_index": li, "ledger_hash": f"H{li}", "ledger_time": 900 + li}


def session(path, ledgers, replay=(), stop=None, resumed=True):
    """One collector run the way run() does it: cut torn tail, rebuild state, session event, messages."""
    torn = drop_torn_tail(path)
    st = VlState().load(path)
    w = Writer(path)
    h = Handler(st, w)
    w(
        {
            "ev": "session",
            "start": 1,
            "resumed": resumed,
            "last_ledger": st.last_ledger,
            "last_t": st.last_t,
            "torn_bytes": torn,
            "bad_lines": st.bad_lines,
            "window_end": WEND,
        }
    )
    for li in replay:  # validations relayed again after a resume -> must be dropped as duplicates
        h.on_message(VM("kA", li), 1)
    for li in ledgers:
        for k in ("kA", "kB", "kC"):
            h.on_message(VM(k, li), li)
        h.on_message(LC(li), li + 0.5)
    if stop:
        w({"ev": "stop", "t": 9, "reason": stop})
    w.fh.close()


def rd(p):
    with open(p, "rb") as fh:
        return fh.read()


def build(torn=b'{"ev":"val","k":"kA","li":1'):
    d = tempfile.mkdtemp()
    p = os.path.join(d, "vl.jsonl")
    session(p, range(100, 106), stop="SIGTERM", resumed=False)
    term = rd(p)
    session(p, range(108, 111), replay=(104, 105))
    with open(p, "ab") as fh:
        fh.write(torn)  # SIGKILL in the middle of a write
    kill = rd(p)
    session(p, range(113, 116), stop="window complete")
    return rd(p), term, kill


def failed(res):
    return [n for n, ok, _ in res if not ok]


class TestResumeCheck(unittest.TestCase):
    def test_positive_all_pass(self):
        res = checks(*build())
        self.assertEqual(failed(res), [])
        self.assertEqual(len(res), 12)
        detail = dict((n, d) for n, _, d in res)
        self.assertIn(
            "106-107 (2, restart), 111-112 (2, restart)",
            detail["ledgers contiguous apart from recorded gaps"],
        )
        self.assertIn("torn_bytes=27", detail["session 3 records the torn bytes cut after SIGKILL"])

    def test_duplicate_validation_detected(self):
        final, term, kill = build()
        final += b'{"ev":"val","k":"kA","li":114,"lh":"H114","st":1,"t":1,"full":true}\n'
        self.assertEqual(
            failed(checks(final, term, kill)),
            ["no duplicate validation entries (key, ledger index, ledger hash)"],
        )

    def test_unrecorded_gap_detected(self):
        final, term, kill = build()
        lines = [x for x in final.split(b"\n") if b'"ev":"ledger","li":114,' not in x]
        self.assertEqual(len(lines), len(final.split(b"\n")) - 1)  # the line really was there
        self.assertEqual(
            failed(checks(b"\n".join(lines), term, kill)), ["ledgers contiguous apart from recorded gaps"]
        )

    def test_term_not_prefix_detected(self):
        final, term, kill = build()
        bad = term.replace(b'"li":101', b'"li":191', 1)
        self.assertNotEqual(bad, term)
        self.assertEqual(
            failed(checks(final, bad, kill)),
            ["SIGTERM snapshot is a byte-identical prefix of the final file"],
        )

    def test_torn_bytes_mismatch_detected(self):
        final, term, kill = build()
        self.assertEqual(
            failed(checks(final, term, kill + b"xx")), ["session 3 records the torn bytes cut after SIGKILL"]
        )

    def test_missing_stop_detected(self):
        final, term, kill = build()
        cut = final[: final.rfind(b'{"ev":"stop"')]
        self.assertTrue(final.startswith(cut) and len(cut) < len(final))
        self.assertEqual(failed(checks(cut, term, kill)), ["stop events: SIGTERM then window complete"])

    def test_empty_file_fails(self):
        self.assertIn("every line parses as JSON", failed(checks(b"", b"", b"")))
        self.assertIn(
            "no duplicate validation entries (key, ledger index, ledger hash)", failed(checks(b"", b"", b""))
        )


if __name__ == "__main__":
    unittest.main()
