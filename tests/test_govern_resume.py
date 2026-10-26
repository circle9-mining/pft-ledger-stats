"""Tests for govern/gov_resume_check.py on synthetic captures built with the collector's own classes.

Each negative asserts the specific check that must fail (and that the others still pass), and the
positive capture is the same data, so an absence check can never pass on an empty file.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "govern"))

from collect_govern import GovState, Handler, Writer, drop_torn_tail  # noqa: E402
from gov_resume_check import checks  # noqa: E402

WEND = 2000
B = 256 * 100  # flag ledger; B - 1 is the voting ledger


def VM(k, li):
    return {
        "type": "validationReceived",
        "validation_public_key": k,
        "ledger_index": str(li),
        "ledger_hash": f"H{li}",
        "full": True,
    }


def LC(li):
    return {"type": "ledgerClosed", "ledger_index": li, "ledger_hash": f"H{li}", "ledger_time": 900 + li}


def session(path, ledgers, replay=(), stop=None, resumed=True):
    """One collector run the way run() does it: cut torn tail, rebuild state, session event, messages."""
    torn = drop_torn_tail(path)
    st = GovState().load(path)
    w = Writer(path)
    reqs = []
    h = Handler(st, w, lambda cmd, **kw: reqs.append(kw))
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
        for kw in reqs:
            h.on_response(
                {"type": "response", "id": kw["id"], "status": "success", "result": {"node": {}}}, li
            )
        reqs.clear()
    if stop:
        w({"ev": "stop", "t": 9, "reason": stop})
    w.fh.close()


def rd(p):
    with open(p, "rb") as fh:
        return fh.read()


def build(torn=b'{"ev":"vote","k":"kA","li":1'):
    d = tempfile.mkdtemp()
    p = os.path.join(d, "govern.jsonl")
    session(p, range(B - 12, B - 7), stop="SIGTERM", resumed=False)
    term = rd(p)
    session(p, range(B - 6, B - 3))
    with open(p, "ab") as fh:
        fh.write(torn)  # SIGKILL in the middle of a write
    kill = rd(p)
    session(p, range(B - 1, B + 3), replay=(B - 1,), stop="window complete")
    return rd(p), term, kill


def failed(res):
    return [n for n, ok, _ in res if not ok]


class TestGovernResumeCheck(unittest.TestCase):
    def test_positive_all_pass(self):
        res = checks(*build())
        self.assertEqual(failed(res), [])
        self.assertEqual(len(res), 15)
        detail = dict((n, d) for n, _, d in res)
        self.assertIn(
            f"{B - 7}-{B - 7} (1, restart), {B - 3}-{B - 2} (2, restart)",
            detail["ledgers contiguous apart from recorded gaps"],
        )
        self.assertIn("torn_bytes=28", detail["session 3 records the torn bytes cut after SIGKILL"])
        self.assertIn(
            "3 entries, 0 dup", detail["no duplicate vote entries (key, ledger index, ledger hash)"]
        )

    def test_duplicate_vote_detected(self):
        final, term, kill = build()
        final += b'{"ev":"vote","k":"kA","li":%d,"lh":"H%d","am":[],"fee":{},"t":1}\n' % (B - 1, B - 1)
        self.assertEqual(
            failed(checks(final, term, kill)), ["no duplicate vote entries (key, ledger index, ledger hash)"]
        )

    def test_vote_on_non_voting_ledger_detected(self):
        final, term, kill = build()
        final += b'{"ev":"vote","k":"kA","li":%d,"lh":"H%d","am":[],"fee":{},"t":1}\n' % (B + 1, B + 1)
        self.assertEqual(
            failed(checks(final, term, kill)), ["every vote entry is for a voting ledger ((li+1) % 256 == 0)"]
        )

    def test_missing_snapshot_detected(self):
        final, term, kill = build()
        lines = [x for x in final.split(b"\n") if b'"obj":"FeeSettings"' not in x]
        self.assertEqual(len(lines), len(final.split(b"\n")) - 1)  # the line really was there
        self.assertEqual(
            failed(checks(b"\n".join(lines), term, kill)),
            ["every delivered flag ledger has exactly one Amendments and one FeeSettings snapshot"],
        )

    def test_window_without_boundary_detected(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "govern.jsonl")
        session(p, range(B + 10, B + 13), stop="SIGTERM", resumed=False)
        term = rd(p)
        session(p, range(B + 14, B + 16))
        with open(p, "ab") as fh:
            fh.write(b'{"ev":')
        kill = rd(p)
        session(p, range(B + 18, B + 20), stop="window complete")
        f = failed(checks(rd(p), term, kill))
        self.assertIn("window covered a voting ledger and the flag ledger after it", f)
        self.assertIn("no duplicate vote entries (key, ledger index, ledger hash)", f)  # no votes at all

    def test_unrecorded_gap_detected(self):
        final, term, kill = build()
        lines = [x for x in final.split(b"\n") if b'"ev":"ledger","li":%d,' % (B + 1) not in x]
        self.assertEqual(len(lines), len(final.split(b"\n")) - 1)
        self.assertEqual(
            failed(checks(b"\n".join(lines), term, kill)), ["ledgers contiguous apart from recorded gaps"]
        )

    def test_term_not_prefix_detected(self):
        final, term, kill = build()
        bad = term.replace(b'"li":%d' % (B - 11), b'"li":%d' % (B - 99), 1)
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

    def test_no_torn_tail_fails_torn_check(self):
        final, term, kill = build(torn=b"")
        self.assertEqual(
            failed(checks(final, term, kill)), ["session 3 records the torn bytes cut after SIGKILL"]
        )

    def test_missing_stop_detected(self):
        final, term, kill = build()
        cut = final[: final.rfind(b'{"ev":"stop"')]
        self.assertTrue(final.startswith(cut) and len(cut) < len(final))
        self.assertEqual(failed(checks(cut, term, kill)), ["stop events: SIGTERM then window complete"])

    def test_empty_file_fails(self):
        f = failed(checks(b"", b"", b""))
        self.assertIn("every line parses as JSON", f)
        self.assertIn("no duplicate vote entries (key, ledger index, ledger hash)", f)


if __name__ == "__main__":
    unittest.main()
