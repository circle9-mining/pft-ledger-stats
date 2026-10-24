import gzip
import hashlib
import importlib.util
import json
import os
import tempfile
import unittest

STATE = os.path.join(os.path.dirname(__file__), "..", "state")


def _load(name):
    # by path: the repo root also has an analyze.py (timing tool)
    spec = importlib.util.spec_from_file_location(f"state_{name}", os.path.join(STATE, f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


snapshot = _load("snapshot")
_an = _load("analyze")
account_id_to_address, analyze, bucketize = _an.account_id_to_address, _an.analyze, _an.bucketize
gini = _an.gini
load, top_share, analyze_main = _an.load, _an.top_share, _an.main

LEDGER_HASH = "AB" * 32


def acct(i, bal, oc=0):
    return {"LedgerEntryType": "AccountRoot", "Account": f"rAcct{i:03d}", "Balance": str(bal),
            "OwnerCount": oc, "index": f"{i:064X}"}


def synthetic_state():
    """10 accounts: 9 x 10 PFT and 1 x 910 PFT (total 1000 PFT), 2 trust lines, 1 offer, 1 escrow."""
    objs = [acct(i, 10_000_000, oc=0) for i in range(9)] + [acct(9, 910_000_000, oc=3)]
    objs[0]["OwnerCount"] = 1  # rAcct000 owns one side of a line
    objs += [
        {"LedgerEntryType": "RippleState", "index": "L1" + "0" * 62,
         "LowLimit": {"issuer": "rAcct000"}, "HighLimit": {"issuer": "rAcct009"}},
        {"LedgerEntryType": "RippleState", "index": "L2" + "0" * 62,
         "LowLimit": {"issuer": "rAcct009"}, "HighLimit": {"issuer": "rrrrrrrrrrrrrrrrrrrrBZbvji"}},
        {"LedgerEntryType": "Offer", "Account": "rAcct009", "index": "O1" + "0" * 62},
        {"LedgerEntryType": "Escrow", "Account": "rAcct009", "Amount": "5000000", "index": "E1" + "0" * 62},
        {"LedgerEntryType": "FeeSettings", "ReserveBase": 10_000_000, "ReserveIncrement": 2_000_000,
         "index": "F" * 64},
    ]
    return objs


def header(objs, total_coins=1_005_000_000):
    return {"ledger_index": 100, "ledger_hash": LEDGER_HASH, "account_hash": "CD" * 32,
            "close_time_iso": "2026-10-24T00:00:00Z", "total_coins": total_coins, "objects_paged": len(objs),
            "server_state_reserve_base": 10_000_000, "server_state_reserve_inc": 2_000_000}


class FakeRpc:
    """Serves synthetic ledger_data pages by marker; can fail on a given call number."""

    def __init__(self, objs, fail_at=None, error=None):
        self.objs, self.fail_at, self.error, self.count, self.markers = objs, fail_at, error, 0, []

    def call(self, method, params=None):
        self.count += 1
        if self.fail_at and self.count == self.fail_at:
            raise self.error
        if method == "server_state":
            return {"state": {"validated_ledger": {"seq": 100, "reserve_base": 10_000_000,
                                                   "reserve_inc": 2_000_000}}}
        if method == "ledger":
            return {"ledger": {"ledger_index": "100", "ledger_hash": LEDGER_HASH, "account_hash": "CD" * 32,
                               "total_coins": "1005000000", "close_time": 846000000, "closed": True}}
        assert method == "ledger_data" and params["ledger_index"] == 100
        start = int(params.get("marker", "0"))
        self.markers.append(params.get("marker"))
        end = start + params["limit"]
        r = {"ledger_hash": LEDGER_HASH, "ledger_index": 100, "state": self.objs[start:end]}
        if end < len(self.objs):
            r["marker"] = str(end)
        return r


def sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


class TestStats(unittest.TestCase):
    def test_gini_known_answers(self):
        self.assertAlmostEqual(gini([5, 5, 5, 5]), 0.0)
        self.assertAlmostEqual(gini([0, 0, 0, 1]), 0.75)          # (n-1)/n with n=4
        self.assertAlmostEqual(gini([1, 2, 3, 4]), 0.25)
        self.assertAlmostEqual(gini([10] * 9 + [910]), 0.81)      # synthetic_state balances (PFT)
        self.assertEqual(gini([]), 0.0)

    def test_top_share(self):
        vals = [10] * 9 + [910]
        self.assertAlmostEqual(top_share(vals, 1), 0.91)
        self.assertAlmostEqual(top_share(vals, 10), 1.0)
        self.assertAlmostEqual(top_share(vals, 100), 1.0)
        self.assertAlmostEqual(top_share(list(range(1, 201)), 10), sum(range(191, 201)) / sum(range(1, 201)))

    def test_buckets(self):
        b = bucketize([0, 0, 1, 2, 4, 5, 30, 100], [(0, 0), (1, 1), (2, 4), (5, 9), (10, 99), (100, None)])
        self.assertEqual(b, {"0": 2, "1": 1, "2-4": 2, "5-9": 1, "10-99": 1, "100+": 1})

    def test_account_id_to_address(self):
        self.assertEqual(account_id_to_address("00" * 20), "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
        self.assertEqual(account_id_to_address("00" * 19 + "01"), "rrrrrrrrrrrrrrrrrrrrBZbvji")

    def test_analyze_synthetic(self):
        objs = synthetic_state()
        r = analyze(header(objs), objs)
        b = r["balance_drops"]
        self.assertEqual(r["accounts"], 10)
        self.assertEqual(b["total"], 1_000_000_000)
        self.assertEqual(b["median"], 10_000_000)
        self.assertAlmostEqual(b["gini"], 0.81)
        self.assertAlmostEqual(b["top10_share"], 1.0)
        self.assertTrue(r["object_count_check"]["ok"])
        # reserve: 9 accounts x 10 PFT base (acct000 has OC 1 -> 12 > 10, capped at its 10 PFT balance)
        # + acct009 10 + 3 x 2 = 16 PFT
        self.assertEqual(r["reserve"]["locked_drops"], 9 * 10_000_000 + 16_000_000)
        self.assertEqual(r["reserve"]["spendable_drops"], 1_000_000_000 - 106_000_000)
        self.assertEqual(r["reserve"]["accounts_below_reserve"], 1)
        self.assertTrue(r["reserve"]["matches_server_state"])
        self.assertEqual(r["owner_count_buckets"]["0"], 8)
        self.assertEqual(r["holders"]["lines"]["accounts_with_any"], 2)    # acct000, acct009
        self.assertEqual(r["holders"]["lines"]["objects"], 3)             # acct000 1 + acct009 2
        self.assertEqual(r["holders"]["offers"]["objects"], 1)
        self.assertEqual(r["holders"]["other"]["objects"], 1)             # the escrow
        self.assertEqual(r["owner_keys_without_accountroot"], 1)
        tc = r["total_coins_check"]
        self.assertEqual(tc["held_in_other_objects"], {"Escrow Amount": 5_000_000})
        self.assertEqual(tc["difference"], 0)
        r2 = analyze(header(objs, total_coins=1_005_000_007), objs)
        self.assertEqual(r2["total_coins_check"]["difference"], 7)

    def test_count_mismatch_detected(self):
        objs = synthetic_state()
        h = header(objs)
        h["objects_paged"] += 1
        self.assertFalse(analyze(h, objs)["object_count_check"]["ok"])


class TestSnapshotResume(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.objs = [acct(i, 1_000_000 + i) for i in range(23)]

    def run_to_end(self, out, limit, **kw):
        return snapshot.collect(FakeRpc(self.objs, **kw), out, limit=limit)

    def test_page_size_does_not_change_bytes(self):
        a, n = self.run_to_end(os.path.join(self.tmp, "a"), 5)
        b, _ = self.run_to_end(os.path.join(self.tmp, "b"), 2048)
        self.assertEqual(n, 23)
        self.assertEqual(sha(a), sha(b))
        hdr, objs = load(a)
        self.assertEqual((hdr["objects_paged"], len(objs)), (23, 23))
        self.assertNotIn("pages", hdr)

    def test_marker_resume_after_interrupt_and_torn_line(self):
        ref, _ = self.run_to_end(os.path.join(self.tmp, "ref"), 5)
        out = os.path.join(self.tmp, "r")
        # run 1: stops after 2 pages (as after SIGTERM)
        path, n = snapshot.collect(FakeRpc(self.objs), out, limit=5, max_pages=2)
        self.assertIsNone(path)
        self.assertEqual(n, 10)
        # run 2: crashes on its 2nd ledger_data call (as on a transport error) after 1 more page
        rpc = FakeRpc(self.objs, fail_at=2, error=RuntimeError("docker exec rc=1"))
        with self.assertRaises(RuntimeError):
            snapshot.collect(rpc, out, limit=5)
        self.assertEqual(rpc.markers, ["10"])   # resumed from page 1's marker, not from the start
        # simulate SIGKILL mid-write: torn half line at the end of pages.jsonl
        with open(os.path.join(out, "pages.jsonl"), "a") as fh:
            fh.write('{"page":3,"ledger_hash":"AB')
        rpc = FakeRpc(self.objs)
        path, n = snapshot.collect(rpc, out, limit=5)
        self.assertEqual(rpc.markers, ["15", "20"])
        self.assertEqual(n, 23)
        self.assertEqual(sha(path), sha(ref))     # byte-identical to the uninterrupted run
        # a restart after completion only rewrites the same snapshot
        path2, _ = snapshot.collect(FakeRpc(self.objs), out, limit=5)
        self.assertEqual(sha(path2), sha(ref))

    def test_pinned_ledger_gone_exits_3(self):
        out = os.path.join(self.tmp, "g")
        snapshot.collect(FakeRpc(self.objs), out, limit=5, max_pages=1)
        with self.assertRaises(SystemExit) as cm:
            snapshot.collect(FakeRpc(self.objs, fail_at=1, error=LookupError("lgrNotFound")), out, limit=5)
        self.assertEqual(cm.exception.code, 3)

    def test_foreign_page_rejected(self):
        out = os.path.join(self.tmp, "f")
        snapshot.collect(FakeRpc(self.objs), out, limit=5, max_pages=2)
        p = os.path.join(out, "pages.jsonl")
        with open(p) as fh:
            lines = fh.readlines()
        bad = json.loads(lines[1])
        bad["ledger_index"] = 99
        with open(p, "w") as fh:
            fh.write(lines[0] + json.dumps(bad) + "\n")
        with self.assertRaises(ValueError):
            snapshot.collect(FakeRpc(self.objs), out, limit=5)

    def test_report_deterministic(self):
        objs = synthetic_state()
        snap = os.path.join(self.tmp, "s.jsonl.gz")
        with gzip.GzipFile(snap, "wb", mtime=0) as gz:
            gz.write((json.dumps(header(objs)) + "\n").encode())
            for o in objs:
                gz.write((json.dumps(o) + "\n").encode())
        outs = []
        for d in ("x", "y"):
            out = os.path.join(self.tmp, d)
            self.assertEqual(analyze_main([snap, "--out", out, "--no-charts"]), 0)
            outs.append((sha(os.path.join(out, "REPORT.md")), sha(os.path.join(out, "report.json"))))
        self.assertEqual(outs[0], outs[1])


if __name__ == "__main__":
    unittest.main()
