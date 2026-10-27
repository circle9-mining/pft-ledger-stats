"""Tests for govern/analyze_govern.py.

The real fixture (2026-10-26 probe: voting ledger 7501055, flag ledger 7501056) goes through the
collector's own Handler, so the analyzer sees exactly what the collector writes. Live validators sent no
vote fields in that capture, so every amendment/fee vote, Majority and vote change below is SYNTHETIC.
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "govern"))

from analyze_govern import RIPPLE_EPOCH, analyze, decode_sv, preview, render  # noqa: E402
from collect_govern import GovState, Handler  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "govern", "probe-7501055.jsonl")
SV9 = "1745990380211404800"  # 1.0.9 (real value)
SV4 = "1745990380127518720"  # 1.0.4 (real value)
X, Y = "AA" * 32, "BB" * 32
V1, V2, V3 = 255, 511, 767  # voting ledgers; flag ledgers 256, 512, 768


def real_events():
    with open(FIX) as fh:
        msgs = [json.loads(x) for x in fh]
    out = []
    h = Handler(GovState(), out.append, lambda *a, **k: None)
    for i, m in enumerate(msgs):
        h.on_message(m, 1000.0 + i)
    return out


def unl(keys):
    return {"t": "T", "quorum": 3, "keys": set(keys), "signing": {}, "seq": [1]}


def vote(mk, li, am=(), fee=None, sv=SV9):
    return {
        "ev": "vote",
        "k": "s" + mk,
        "mk": mk,
        "li": li,
        "lh": f"H{li}",
        "full": True,
        "sv": sv,
        "am": list(am),
        "fee": fee or {},
        "extra": [],
        "t": li,
    }


def ledgers(lo, hi):
    return [{"ev": "ledger", "li": i, "lh": f"H{i}", "ct": i, "t": i} for i in range(lo, hi + 1)]


def amend_snap(li, enabled, majs=()):
    node = {"Amendments": list(enabled), "LedgerEntryType": "Amendments"}
    if majs:
        node["Majorities"] = [{"Majority": {"Amendment": a, "CloseTime": ct}} for a, ct in majs]
    return {"ev": "snap", "li": li, "obj": "Amendments", "node": node, "t": li}


def row(s, v):
    return next(r for r in s["per_validator"] if r["validator"] == v)


class TestReal(unittest.TestCase):
    def test_real_fixture_no_votes(self):
        evs = real_events()
        mks = sorted({e["mk"] for e in evs if e["ev"] == "vote"})
        self.assertEqual(len(mks), 3)
        s = analyze(evs, unl(mks + ["nNeverSeen"]))
        self.assertEqual((s["voting_ledgers_seen"], s["voting_ledgers_expected"]), (1, 1))
        self.assertEqual((s["flag_ledgers_seen"], s["flag_ledgers_expected"]), (1, 1))
        self.assertEqual(s["validators"], 3)
        self.assertEqual(s["unl_seen"], 3)
        self.assertEqual(s["amendments_voted"], [])
        for r in s["per_validator"]:  # validated the voting ledger, voted for nothing (no-vote validators)
            self.assertEqual(
                (r["voting_ledgers_validated"], r["amendments_voted"], r["missing_votes"]), (1, [], [])
            )
            self.assertEqual(r["server_version"], "1.0.9")
        self.assertEqual(s["unl_tally"][0]["unl_validations"], 3)  # nNeverSeen: no validation
        self.assertIn("| 7501055 | 3/4 | 0 | 0.0% | - |", render(s, "OS", "yes"))

    def test_decode_sv(self):
        self.assertEqual(decode_sv(SV9), "1.0.9")
        self.assertEqual(decode_sv(SV4), "1.0.4")
        self.assertEqual(decode_sv("1745990380060409856"), "1.0.0")
        self.assertIsNone(decode_sv(None))


class TestSynthetic(unittest.TestCase):
    def test_vote_change_and_yes_share(self):
        evs = ledgers(250, 520) + [vote("A", V1, [X]), vote("A", V2, [X, Y])]
        s = analyze(evs, unl(["A"]))
        r = row(s, "A")
        self.assertEqual(r["vote_changes"], [{"li": V2, "added": [Y], "removed": []}])
        self.assertEqual(r["yes_share_window"], {X: 1.0, Y: 0.5})
        self.assertEqual(r["amendments_voted"], [X, Y])

    def test_active_span_vs_window(self):
        # B joins at the second voting ledger: window share 1/3, span share 1/1, no missing votes (not UNL)
        evs = ledgers(250, 770) + [vote("A", li, [X]) for li in (V1, V2, V3)] + [vote("B", V3, [X])]
        s = analyze(evs, unl(["A"]))
        b = row(s, "B")
        self.assertAlmostEqual(b["yes_share_window"][X], 1 / 3)
        self.assertEqual(b["yes_share_span"][X], 1.0)
        self.assertEqual(b["missing_votes"], [])

    def test_unl_missing_vote(self):
        evs = ledgers(250, 770) + [vote("A", li) for li in (V1, V3)] + [vote("B", li) for li in (V1, V2, V3)]
        s = analyze(evs, unl(["A", "B"]))
        self.assertEqual(row(s, "A")["missing_votes"], [V2])
        self.assertEqual(row(s, "B")["missing_votes"], [])

    def test_missing_vote_ignores_undelivered_ledgers(self):
        # V2 itself was never delivered (collector gap): nobody can be blamed for it
        evs = [e for e in ledgers(250, 770) if e["li"] != V2] + [vote("A", li) for li in (V1, V3)]
        s = analyze(evs, unl(["A"]))
        self.assertEqual(s["voting_ledgers_seen"], 2)
        self.assertEqual(row(s, "A")["missing_votes"], [])

    def test_unl_threshold(self):
        keys = ["A", "B", "C", "D", "E"]
        evs = ledgers(250, 520)
        evs += [vote(k, V1, [X]) for k in keys[:4]] + [vote("E", V1)]  # 4/5 = 80% -> majority
        evs += [vote(k, V2, [X]) for k in keys[:3]] + [vote(k, V2) for k in keys[3:]]  # 3/5 = 60%
        s = analyze(evs, unl(keys))
        t = {x["li"]: x for x in s["unl_tally"]}
        self.assertEqual(t[V1]["over_threshold"], [X])
        self.assertEqual(t[V2]["over_threshold"], [])
        self.assertEqual(t[V2]["share"][X], 0.6)

    def test_majority_appear_disappear_and_projection(self):
        ct = 846339000
        evs = ledgers(250, 1030) + [
            amend_snap(256, ["E1"]),
            amend_snap(512, ["E1"], [(X, ct)]),
            amend_snap(768, ["E1"], [(X, ct), (Y, ct + 10)]),
            amend_snap(1024, ["E1"], [(Y, ct + 10)]),
        ]
        s = analyze(evs, unl([]))
        self.assertEqual(
            [(e["li"], e["event"], e["amendment"]) for e in s["majority_events"]],
            [(512, "appeared", X), (768, "appeared", Y), (1024, "disappeared", X)],
        )
        self.assertEqual(s["majority_events"][0]["projected_activation"], "2026-11-09T14:10:00Z")
        self.assertEqual(s["majority_events"][0]["since"], "2026-10-26T14:10:00Z")
        self.assertEqual(list(s["majorities_current"]), [Y])
        self.assertEqual(ct + RIPPLE_EPOCH, 1793023800)  # 2026-10-26T14:10:00Z

    def test_build_blocked_risk(self):
        evs = ledgers(250, 260) + [vote("A", V1), vote("B", V1, sv=SV4), vote("C", V1, sv=SV4)]
        s = analyze(evs, unl(["A", "B"]))
        self.assertFalse(row(s, "A")["build_blocked_risk"])
        self.assertTrue(row(s, "B")["build_blocked_risk"])
        self.assertTrue(row(s, "C")["build_blocked_risk"])  # non-UNL on an old build, flagged too
        self.assertEqual(s["newest_unl_build"], "1.0.9")

    def test_fee_votes_and_fee_settings(self):
        fee = {"base_fee": 20}
        evs = ledgers(250, 520) + [vote("A", V1, fee=fee), vote("A", V2, fee=fee), vote("B", V1)]
        evs += [
            {
                "ev": "snap",
                "li": 256,
                "obj": "FeeSettings",
                "t": 1,
                "node": {"BaseFee": "a", "ReserveBase": 10, "index": "x", "LedgerEntryType": "FeeSettings"},
            },
            {
                "ev": "snap",
                "li": 512,
                "obj": "FeeSettings",
                "t": 2,
                "node": {"BaseFee": "14", "ReserveBase": 10, "index": "x", "LedgerEntryType": "FeeSettings"},
            },
        ]
        s = analyze(evs, unl(["A", "B"]))
        self.assertEqual(row(s, "A")["fee_votes"], {'{"base_fee": 20}': 2})
        self.assertEqual(row(s, "B")["fee_votes"], {})
        self.assertEqual(s["fee_voters"], 1)
        self.assertEqual([f["li"] for f in s["fee_settings"]], [256, 512])  # change recorded once

    def test_vote_for_non_validated_hash_not_counted(self):
        losing = dict(vote("A", V1, [X]), lh="OTHER")
        s = analyze(ledgers(250, 260) + [losing, vote("B", V1)], unl(["A", "B"]))
        self.assertEqual(s["vote_entries_other_hash"], 1)
        self.assertEqual(row(s, "B")["voting_ledgers_validated"], 1)
        self.assertNotIn("A", [r["validator"] for r in s["per_validator"]])


class TestPreview(unittest.TestCase):
    def test_preview_cuts_on_line_boundary_with_marker(self):
        md = "".join(f"| {i:07d} | 25/25 |\n" for i in range(400))
        out = preview(md, limit=105)  # 20-char lines: 105 falls mid-line
        body, marker = out.rsplit("... (", 1)
        self.assertTrue(body.endswith("|\n"))
        self.assertTrue(md.startswith(body))
        self.assertEqual(marker, f"{400 - body.count(chr(10))} more lines in REPORT.md)\n")

    def test_preview_short_report_unchanged(self):
        self.assertEqual(preview("a\nb\n", limit=100), "a\nb\n")


if __name__ == "__main__":
    unittest.main()
