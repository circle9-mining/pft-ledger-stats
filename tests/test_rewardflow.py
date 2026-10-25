import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rewardflow"))

import analyze_rewardflow as an  # noqa: E402
import collect_rewardflow as co  # noqa: E402
import crosscheck_rewardflow as cx  # noqa: E402

PAYER = "rPAYERxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
MEMO = {"Memo": {"MemoType": "70662E707472", "MemoFormat": "7634",
                 "MemoData": "0A02516D10013A0C7461736B5F6162633132333440"}}  # ...task_abc1234@


def entry(seq, ledger, dest, amount, delivered=None, result="tesSUCCESS", account=PAYER, flags=0, fee="12"):
    delivered = amount if delivered is None else delivered
    h = f"{seq:064X}"
    meta = {"TransactionResult": result, "AffectedNodes": [{"ModifiedNode": {
        "LedgerEntryType": "AccountRoot",
        "FinalFields": {"Account": PAYER, "Balance": "0"},
        "PreviousFields": {"Balance": str(int(fee) + (int(delivered) if result == "tesSUCCESS" else 0))},
    }}]}
    if result == "tesSUCCESS":
        meta["delivered_amount"] = delivered
    if account != PAYER:  # incoming: payer balance goes up by delivered
        meta["AffectedNodes"][0]["ModifiedNode"]["PreviousFields"]["Balance"] = "0"
        meta["AffectedNodes"][0]["ModifiedNode"]["FinalFields"]["Balance"] = delivered
    tx = {"Account": account, "Destination": dest, "Amount": amount, "Fee": fee, "Flags": flags,
          "TransactionType": "Payment", "hash": h, "ledger_index": ledger, "date": 846000000 + ledger * 4,
          "Memos": [MEMO]}
    return {"tx": tx, "meta": meta, "validated": True}


class FakeServer:
    """account_tx over a fixed history with integer markers; counts calls; can inject errors."""

    def __init__(self, entries, lo=100, hi=200, fail_first=0, balances=None, flaky=None):
        self.entries, self.lo, self.hi = entries, lo, hi
        self.flaky = flaky or {}  # (method, ledger_index or hash) -> number of transient bad answers
        self.fail_first = fail_first
        self.calls = []
        self.balances = balances or {}

    def post(self, body):
        req = json.loads(body)
        m, p = req["method"], req["params"][0]
        self.calls.append((m, p))
        if self.fail_first:
            self.fail_first -= 1
            return {"result": {"status": "error", "error": "slowDown"}}
        if m == "server_info":
            return {"result": {"info": {"complete_ledgers": f"{self.lo}-{self.hi}"}, "status": "success"}}
        if m == "account_tx":
            sel = [e for e in self.entries if p["ledger_index_min"] <= e["tx"]["ledger_index"]
                   <= p["ledger_index_max"]]
            start = p.get("marker", 0)
            page = sel[start:start + p["limit"]]
            res = {"status": "success", "transactions": page, "ledger_index_min": p["ledger_index_min"],
                   "ledger_index_max": p["ledger_index_max"]}
            if start + p["limit"] < len(sel):
                res["marker"] = start + p["limit"]
            return {"result": res}
        key = (m, p.get("ledger_index", p.get("transaction")))
        if self.flaky.get(key):
            self.flaky[key] -= 1
            if m == "tx":  # validated answer without metadata, as seen live
                e = next(e for e in self.entries if e["tx"]["hash"] == p["transaction"])
                return {"result": dict(e["tx"], validated=True, status="success")}
            return {"result": {"status": "error", "error": "lgrNotFound"}}
        if m == "tx":
            e = next(e for e in self.entries if e["tx"]["hash"] == p["transaction"])
            return {"result": dict(e["tx"], meta=e["meta"], validated=True, status="success")}
        if m == "account_info":
            if p["ledger_index"] < self.lo:
                return {"result": {"status": "error", "error": "lgrNotFound"}}
            bal = str(self.balances[p["ledger_index"]])
            return {"result": {"status": "success", "account_data": {"Balance": bal}}}
        raise AssertionError(m)


def history():
    return [
        entry(1, 110, "rAAAAAAzzz", "5000000"),
        entry(2, 120, "rBBBBBBzzz", "5000000"),
        entry(3, 120, "rAAAAAAzzz", "4500000"),
        entry(4, 130, "rCCCCCCzzz", "9000000", delivered="3000000", flags=co_flag()),  # partial
        entry(5, 140, "rDDDDDDzzz", "5000000", result="tecUNFUNDED_PAYMENT"),
        entry(6, 150, PAYER, "100000000", account="rFUNDERzzz"),  # incoming funding
        entry(7, 160, "rAAAAAAzzz", "55000000"),
    ]


def co_flag():
    return an.TF_PARTIAL


def rpc_for(server):
    return co.Rpc("fake://", gap=0, post=server.post, sleep=lambda s: None)


class Collect(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.out = os.path.join(self.d.name, "data.jsonl")

    def tearDown(self):
        self.d.cleanup()

    def lines(self):
        with open(self.out) as fh:
            return [json.loads(x) for x in fh]

    def test_pagination_follows_marker_to_end(self):
        s = FakeServer(history())
        done = co.collect(PAYER, self.out, rpc_for(s), limit=2)
        pages = [c for c in s.calls if c[0] == "account_tx"]
        self.assertEqual(len(pages), 4)  # 7 tx / limit 2
        self.assertEqual([p.get("marker") for _, p in pages], [None, 2, 4, 6])
        self.assertTrue(all(p["forward"] for _, p in pages))
        self.assertEqual(done["tx_count"], 7)
        self.assertEqual((done["ledger_min"], done["ledger_max"]), (100, 200))
        kinds = [x["kind"] for x in self.lines()]
        self.assertEqual(kinds, ["range"] + ["tx"] * 7 + ["done"])

    def test_resume_after_stop_has_no_duplicates_and_same_content(self):
        ref = os.path.join(self.d.name, "ref.jsonl")
        co.collect(PAYER, ref, rpc_for(FakeServer(history())), limit=2)
        s = FakeServer(history())
        # stop after page 1 = tx 1-2; tx 3 sits in the same ledger (120) as tx 2, so a resume that
        # started after ledger 120 would lose it, and one that started at 120 must skip tx 2
        self.assertIsNone(co.collect(PAYER, self.out, rpc_for(s), limit=2, max_pages=1))
        s2 = FakeServer(history(), hi=999)  # server grew: pinned range must be kept
        co.collect(PAYER, self.out, rpc_for(s2), limit=2)
        got = [x for x in self.lines() if x["kind"] == "tx"]
        with open(ref) as fh:
            want = [json.loads(x) for x in fh if '"kind": "tx"' in x]
        self.assertEqual([x["hash"] for x in got], [x["hash"] for x in want])
        self.assertEqual(len({x["hash"] for x in got}), 7)
        first = [p for m, p in s2.calls if m == "account_tx"][0]
        self.assertEqual(first["ledger_index_min"], 120)  # restarts at last stored ledger (inclusive)
        self.assertEqual(first["ledger_index_max"], 200)
        self.assertEqual(self.lines()[-1]["dup_skipped_this_run"], 1)

    def test_torn_last_line_is_repaired(self):
        co.collect(PAYER, self.out, rpc_for(FakeServer(history())), limit=2, max_pages=1)
        with open(self.out, "a") as fh:
            fh.write('{"kind": "tx", "hash": "TOR')
        co.collect(PAYER, self.out, rpc_for(FakeServer(history())), limit=2)
        hashes = [x["hash"] for x in self.lines() if x["kind"] == "tx"]
        self.assertEqual(len(hashes), 7)
        self.assertEqual(len(set(hashes)), 7)

    def test_finished_file_is_not_recollected(self):
        co.collect(PAYER, self.out, rpc_for(FakeServer(history())))
        s = FakeServer(history())
        co.collect(PAYER, self.out, rpc_for(s))
        self.assertEqual(s.calls, [])

    def test_retry_backoff_on_slowdown(self):
        s = FakeServer(history(), fail_first=2)
        waits = []
        rpc = co.Rpc("fake://", gap=0, backoff=1.5, post=s.post, sleep=waits.append)
        co.collect(PAYER, self.out, rpc)
        self.assertEqual(rpc.retried, 2)
        self.assertIn(1.5, waits)
        self.assertIn(3.0, waits)

    def test_non_retryable_error_raises(self):
        def post(body):
            return {"result": {"status": "error", "error": "actNotFound"}}
        with self.assertRaises(co.RpcError):
            co.Rpc("fake://", gap=0, post=post, sleep=lambda s: None).call("account_tx", {})

    def test_refuses_write_methods(self):
        with self.assertRaises(ValueError):
            co.Rpc("fake://").call("submit", {})

    def test_record_fields(self):
        r = co.record(history()[3], PAYER)
        self.assertEqual(r["amount"], "9000000")
        self.assertEqual(r["delivered_amount"], "3000000")
        self.assertEqual(r["meta_balance_delta"], -3000012)
        self.assertEqual(r["memos"], [{"type": "pf.ptr", "format": "v4", "kind": "task"}])
        self.assertEqual(r["direction"], "out")
        self.assertEqual(co.record(history()[5], PAYER)["direction"], "in")

    def test_memo_kind_real_shape(self):
        # live MemoData shape: protobuf field 7 (0x3A), length 0x2C (= ',' printable), then airdrop_<hex>
        self.assertEqual(co.memo_kind("10013A2C61697264726F705F316263643237"), "airdrop")
        self.assertEqual(co.memo_kind("0A02516D"), "other")  # negative control: no <word>_<hex> token
        self.assertEqual(co.memo_kind(None), None)
        self.assertEqual(co.memo_kind("ZZ"), "undecodable")


class Analyze(unittest.TestCase):
    def test_delivered_amount_not_amount_and_skips(self):
        txs = [co.record(e, PAYER) for e in history()]
        a = an.analyze(txs, PAYER)
        # 5 + 5 + 4.5 + 3 (delivered, not 9) + 55; failed and incoming excluded
        self.assertEqual(a["count"], 5)
        self.assertAlmostEqual(a["total_pft"], 72.5)
        self.assertEqual(len(a["failed"]), 1)
        self.assertEqual(a["partial"], 1)
        self.assertEqual(a["other"], {"in:Payment": 1})
        self.assertEqual(a["unique"], 3)
        self.assertAlmostEqual(a["repeat_rate"], 1 / 3)
        self.assertEqual(a["size_max"], 55.0)
        self.assertEqual(a["size_pct"][50], 5.0)
        self.assertEqual(a["size_hist"]["1 - <5"], 2)  # 4.5 and 3 (delivered) -> same bucket
        self.assertEqual(a["size_hist"]["5 - <10"], 2)
        self.assertEqual(a["size_hist"]["10 - <100"], 1)
        self.assertEqual(sum(a["size_hist"].values()), a["count"])
        self.assertEqual(a["gaps"], [40.0, 0.0, 40.0, 120.0])

    def test_concentration_metrics(self):
        self.assertAlmostEqual(an.gini([1, 1, 1, 1]), 0.0)
        self.assertAlmostEqual(an.gini([0, 0, 0, 10]), 0.75)
        txs = [co.record(entry(i, 100 + i, f"r{i}", "1000000"), PAYER) for i in range(4)]
        a = an.analyze(txs, PAYER)
        self.assertAlmostEqual(a["hhi"], 0.25)
        self.assertEqual(a["top10_share"], 1.0)
        self.assertEqual(a["repeat_rate"], 0.0)

    def test_negative_control_no_success_payments(self):
        txs = [co.record(entry(1, 110, "rX", "1", result="tecPATH_DRY"), PAYER)]
        a = an.analyze(txs, PAYER)
        self.assertEqual((a["count"], a["total_pft"], len(a["failed"])), (0, 0, 1))

    def test_report_truncates_addresses(self):
        txs = [co.record(e, PAYER) for e in history()]
        a = an.analyze(txs, PAYER)
        rng = {"ledger_min": 100, "ledger_max": 200, "complete_ledgers": "100-200", "started": "x",
               "rpc": "y"}
        md = an.render(a, rng, None, PAYER, [])
        self.assertNotIn("rAAAAAAzzz", md)
        self.assertIn("`rAAAAA`", md)  # presence control for the absence check above
        self.assertNotIn(PAYER, md)
        self.assertIn("INCOMPLETE", md)


class Crosscheck(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.out = os.path.join(self.d.name, "data.jsonl")
        # payer balance after each ledger: start 1000 PFT at ledger 100, apply meta deltas
        bal, b = {}, 1_000_000_000
        deltas = {}
        for e in history():
            deltas[e["tx"]["ledger_index"]] = deltas.get(e["tx"]["ledger_index"], 0) + co.balance_delta(
                e["meta"], PAYER)
        for li in range(100, 201):
            b += deltas.get(li, 0)
            bal[li] = b
        self.bal = bal

    def tearDown(self):
        self.d.cleanup()

    def run_cx(self, server, tamper=None):
        co.collect(PAYER, self.out, rpc_for(FakeServer(history())))
        if tamper:
            with open(self.out) as fh:
                rows = [json.loads(x) for x in fh]
            tamper(rows)
            with open(self.out, "w") as fh:
                fh.writelines(json.dumps(r) + "\n" for r in rows)
        return cx.main([self.out, "--count", "4"], rpc=rpc_for(server), sleep=lambda s: None)

    def test_pass_and_impossible_range_reported(self):
        out = []
        s = FakeServer(history(), balances=self.bal)
        self.assertEqual(self.run_cx(s), 0)
        rng, txs = cx.load(self.out)
        ok = cx.reconcile(rpc_for(s), txs, PAYER, cx.plan_ranges(rng, txs, 3), out=out.append)
        self.assertTrue(ok)
        self.assertTrue(any("IMPOSSIBLE" in x and "(99,200]" in x for x in out))
        self.assertEqual(sum("RECONCILED" in x for x in out), 4)

    def test_transient_answers_are_retried_and_logged(self):
        out = []
        co.collect(PAYER, self.out, rpc_for(FakeServer(history())))
        rng, txs = cx.load(self.out)
        h = txs[0]["hash"]
        s = FakeServer(history(), balances=self.bal, flaky={("account_info", 100): 2, ("tx", h): 1})
        rpc = rpc_for(s)
        self.assertTrue(cx.reconcile(rpc, txs, PAYER, [(100, 200)], out=out.append))
        self.assertTrue(cx.refetch(rpc, [txs[0]], PAYER, 1, 1, out=out.append))
        self.assertEqual(sum(x.startswith("RETRY account_info") for x in out), 2)
        self.assertEqual(sum(x.startswith("RETRY tx") for x in out), 1)
        self.assertTrue(any(x.startswith("RANGE (100,200] RECONCILED") for x in out))
        self.assertTrue(any(x.startswith("MATCH " + h) for x in out))

    def test_persistent_transient_is_impossible_or_mismatch(self):
        out = []
        co.collect(PAYER, self.out, rpc_for(FakeServer(history())))
        rng, txs = cx.load(self.out)
        h = txs[0]["hash"]
        s = FakeServer(history(), balances=self.bal, flaky={("account_info", 150): 9, ("tx", h): 9})
        rpc = rpc_for(s)
        self.assertFalse(cx.reconcile(rpc, txs, PAYER, [(150, 200)], out=out.append))  # nothing checkable
        self.assertFalse(cx.refetch(rpc, [txs[0]], PAYER, 1, 1, out=out.append))
        self.assertTrue(any(x.startswith("RANGE (150,200] IMPOSSIBLE") for x in out))
        self.assertTrue(any(x.startswith("MISMATCH " + h) for x in out))

    def test_tampered_field_is_caught(self):
        def tamper(rows):
            for r in rows:
                if r.get("kind") == "tx" and r["result"] == "tesSUCCESS" and r["direction"] == "out":
                    r["delivered_amount"] = "1"
        self.assertEqual(self.run_cx(FakeServer(history(), balances=self.bal), tamper), 1)

    def test_balance_mismatch_is_caught(self):
        bad = dict(self.bal)
        bad[200] += 1
        self.assertEqual(self.run_cx(FakeServer(history(), balances=bad)), 1)

    def test_derived_delta(self):
        r = [co.record(e, PAYER) for e in history()]
        self.assertEqual(sum(cx.derived_delta(t, PAYER) for t in r),
                         sum(t["meta_balance_delta"] for t in r))


if __name__ == "__main__":
    unittest.main()
