import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "govern"))

from collect_govern import OBJECTS, GovState, Handler, is_flag, is_voting, send  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "govern", "probe-7501055.jsonl")


def real():
    """Real stream messages captured by the 2026-10-26 probe: ledgerClosed 7501054..7501056 (7501056 is a
    flag ledger), 3 validations of voting ledger 7501055, 2 of non-voting 7501054.
    None of them carries vote fields."""
    with open(FIX) as fh:
        return [json.loads(x) for x in fh]


def run(msgs, state=None):
    out, reqs = [], []
    st = state or GovState()
    h = Handler(st, out.append, lambda cmd, **kw: reqs.append((cmd, kw)))
    for i, m in enumerate(msgs):
        h.on_message(m, 1000.0 + i)
    return out, reqs, h, st


class TestFlagMath(unittest.TestCase):
    def test_voting_and_flag(self):
        self.assertTrue(is_flag(7501056))
        self.assertTrue(is_voting(7501055))
        self.assertFalse(is_voting(7501054))
        self.assertFalse(is_voting(7501056))
        self.assertFalse(is_flag(7501055))


class TestRealFixture(unittest.TestCase):
    def test_voting_validations_recorded_without_vote_fields(self):
        out, _, _, _ = run(real())
        votes = [e for e in out if e["ev"] == "vote"]
        self.assertEqual(len(votes), 3)  # only ledger 7501055; the 7501054 validations are not voting
        self.assertTrue(all(e["li"] == 7501055 for e in votes))
        self.assertTrue(all(e["am"] == [] and e["fee"] == {} for e in votes))
        self.assertTrue(all(e["extra"] == [] for e in votes))  # every real field is in KNOWN
        self.assertTrue(all(e["sv"] for e in votes))

    def test_flag_ledger_requests_both_objects(self):
        _, reqs, _, _ = run(real())
        self.assertEqual(
            sorted((c, kw["id"], kw["index"], kw["ledger_index"]) for c, kw in reqs),
            sorted(("ledger_entry", f"snap:7501056:{o}", oid, 7501056) for o, oid in OBJECTS.items()),
        )

    def test_send_allows_only_read_commands(self):
        class WS:
            sent = []

            def send(self, s):
                self.sent.append(s)

        send(WS(), "ledger_entry", index="x")
        with self.assertRaises(ValueError):
            send(WS(), "submit", tx_blob="00")


class TestSyntheticVotes(unittest.TestCase):
    """Vote fields are SYNTHETIC here (no live validator sent any on 2026-10-26):
    real message shape plus added fields."""

    def test_amendment_and_fee_votes_kept(self):
        m = dict(real()[3])
        m["amendments"] = ["AA" * 32, "BB" * 32]
        m["base_fee"], m["reserve_base"] = 20, 5000000
        m["new_field_x"] = 1
        out, _, _, _ = run([m])
        e = out[0]
        self.assertEqual(e["am"], ["AA" * 32, "BB" * 32])
        self.assertEqual(e["fee"], {"base_fee": 20, "reserve_base": 5000000})
        self.assertEqual(e["extra"], ["new_field_x"])


class TestResume(unittest.TestCase):
    def test_duplicates_and_resume_overlap_dropped(self):
        msgs = real()
        out1, _, _, st = run(msgs)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "govern.jsonl")
            with open(p, "w") as fh:
                fh.writelines(json.dumps(e) + "\n" for e in out1)
                fh.write('{"ev":"vote","k":')  # torn line: counted, not fatal
            st2 = GovState().load(p)
        self.assertEqual(st2.bad_lines, 1)
        self.assertEqual(st2.last_ledger, 7501056)
        out2, reqs2, h2, _ = run(msgs, st2)  # the same messages replayed after a restart
        self.assertEqual([e for e in out2 if e["ev"] in ("vote", "ledger")], [])
        self.assertEqual(h2.dups, 3)
        self.assertEqual(
            len(reqs2), 0
        )  # flag ledger 7501056 already recorded: no new ledger event, no request

    def test_snapshot_retried_after_restart_if_missing(self):
        out, reqs, h, st = run(real())
        h.on_response(
            {
                "type": "response",
                "id": "snap:7501056:Amendments",
                "status": "success",
                "result": {"node": {"LedgerEntryType": "Amendments", "Amendments": []}},
            },
            2000.0,
        )
        h.on_response(
            {"type": "response", "id": "snap:7501056:FeeSettings", "status": "error", "error": "lgrNotFound"},
            2000.0,
        )
        snaps = [e for e in out if e["ev"] == "snap"]
        self.assertEqual(
            [("node" in e, e.get("error")) for e in snaps], [(True, None), (False, "lgrNotFound")]
        )
        self.assertEqual(st.snaps, {(7501056, "Amendments")})
        self.assertFalse(h.on_response({"type": "response", "id": "sub", "status": "success"}, 2001.0))

    def test_gap_reason_restart(self):
        msgs = real()
        st = GovState()
        st.apply({"ev": "ledger", "li": 7501050, "t": 1})
        out, _, _, _ = run(msgs, st)
        gaps = [e for e in out if e["ev"] == "gap"]
        self.assertEqual(
            gaps,
            [
                {
                    "ev": "gap",
                    "from": 7501051,
                    "to": 7501053,
                    "count": 3,
                    "reason": "restart",
                    "t": gaps[0]["t"],
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
