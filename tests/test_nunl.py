"""nunl/ unit tests: synthetic fixtures and fakes only (no network, no node)."""

import hashlib
import json
import os
import sys

import pytest

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(here, "..", "nunl"))
import analyze_nunl as A  # noqa: E402
import collect_nunl as C  # noqa: E402
import crosscheck_nunl as X  # noqa: E402

KA = "ED" + "11" * 32
KB = "ED" + "22" * 32
EMPTY = {"disabled": [], "to_disable": None, "to_reenable": None}


def flag(li, mods=(), nunl=None, nxt="same", ct=None):
    return {
        "ev": "flag",
        "li": li,
        "lh": f"H{li}",
        "ct": ct if ct is not None else li * 3,
        "ntx": len(mods),
        "mods": list(mods),
        "nunl": nunl,
        "next": nunl if nxt == "same" else nxt,
    }


def mod(v, disabling, li):
    return {"validator": v, "disabling": disabling, "seq": li, "hash": f"T{li}{disabling}"}


def session(lo, hi):
    return {"ev": "session", "from": lo, "to": hi}


def lifecycle():
    """KA voted off at 512, listed from 768, voted back at 1024, removed at 1280."""
    return [
        session(256, 1290),
        flag(256),
        flag(512, [mod(KA, 1, 512)], {"disabled": [], "to_disable": KA, "to_reenable": None}),
        flag(768, [], {"disabled": [KA], "to_disable": None, "to_reenable": None}),
        flag(1024, [mod(KA, 0, 1024)], {"disabled": [KA], "to_disable": None, "to_reenable": KA}),
        flag(1280, [], EMPTY),
    ]


# ---------- thresholds (postfiatd NegativeUNLVote.h) ----------


def test_threshold_constants_match_postfiatd():
    assert (A.FLAG, A.LOW, A.HIGH, A.MIN_LOCAL, A.MAX_LISTED) == (256, 128, 204, 230, 0.25)


@pytest.mark.parametrize(
    "score,listed,status",
    [
        (0, False, "DISABLE_CANDIDATE"),
        (127, False, "DISABLE_CANDIDATE"),
        (128, False, "WATCH"),  # < LOW is strict: exactly 50% is NOT a candidate
        (152, False, "WATCH"),
        (153, False, "OK"),  # WATCH = 256 * 60 // 100 = 153 (report-only margin)
        (256, False, "OK"),
        (204, True, "STAYS_LISTED"),  # > HIGH is strict: exactly 80% does NOT re-enable
        (205, True, "REENABLE"),
        (0, True, "STAYS_LISTED"),
    ],
)
def test_classify_boundaries(score, listed, status):
    assert A.classify(score, listed) == status


# ---------- key encoding ----------


def b58decode(s):
    n = 0
    for c in s:
        n = n * 58 + A.B58.index(c)
    return n.to_bytes(38, "big")


def test_node_key_roundtrip_real_unl_key():
    k = "nHBxQ3srN8ErMvfMgsJ3jycSSaMFyHMk5WbrJVUTXPZgNh1VdbHH"  # a trusted key from the UNL snapshot
    raw = b58decode(k)
    assert raw[0] == 0x1C
    assert hashlib.sha256(hashlib.sha256(raw[:34]).digest()).digest()[:4] == raw[34:]
    assert A.node_key_b58(raw[1:34].hex().upper()) == k
    assert A.key(raw[1:34].hex()) == k and A.key(k) == k


# ---------- history reconstruction ----------


def test_lifecycle_timeline_and_spans():
    h = A.history(lifecycle())
    assert h["check_failures"] == []
    assert h["scanned_flags"] == h["expected_flags"] == 5 and h["missing"] == []
    ka = A.key(KA)
    assert [e["action"] for e in h["events"]] == ["vote_disable", "vote_reenable"]
    assert [t[:2] for t in h["timeline"][ka]] == [
        ["vote_disable", 512],
        ["disabled", 768],
        ["vote_reenable", 1024],
        ["reenabled", 1280],
    ]
    assert h["spans"][ka] == [{"from": 768, "to": 1280, "ledgers": 512, "hours": round(512 * 3 / 3600, 2)}]
    assert h["current"] == {"disabled": [], "to_disable": None, "to_reenable": None}


def test_open_span_and_current_state():
    evs = lifecycle()[:4]
    h = A.history(evs)
    assert h["current"]["disabled"] == [A.key(KA)]
    assert h["spans"][A.key(KA)][0]["open"] is True


def test_corrupted_list_fails_check3():
    evs = lifecycle()
    evs[3]["nunl"] = evs[3]["next"] = {"disabled": [KA, KB], "to_disable": None, "to_reenable": None}
    h = A.history(evs)
    assert any("DisabledValidators" in x and "F768" in x for x in h["check_failures"])


def test_mod_without_pending_fails_check2():
    evs = lifecycle()
    evs[2]["nunl"] = evs[2]["next"] = EMPTY
    assert any("UNLModify txs" in x for x in A.history(evs)["check_failures"])


def test_change_between_flag_and_next_fails_check1():
    evs = lifecycle()
    evs[3]["next"] = EMPTY
    assert any("F+1 differs" in x for x in A.history(evs)["check_failures"])


def test_missing_flag_detected():
    evs = [e for e in lifecycle() if e.get("li") != 768]
    h = A.history(evs)
    assert h["missing"] == [768]


def test_main_exits_1_on_missing_flag(tmp_path):
    p = tmp_path / "h.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in lifecycle() if e.get("li") != 1024) + "\n")
    assert A.main(["--hist", str(p), "--out", str(tmp_path / "o")]) == 1


def test_empty_history_report_states_scanned_range(tmp_path):
    p = tmp_path / "h.jsonl"
    p.write_text(
        "\n".join(
            json.dumps(e)
            for e in [session(1000, 2100), flag(1024), flag(1280), flag(1536), flag(1792), flag(2048)]
        )
        + "\n"
    )
    rc = A.main(["--hist", str(p), "--out", str(tmp_path / "o")])
    md = (tmp_path / "o" / "REPORT.md").read_text()
    assert rc == 0
    assert "**5/5** in ledger range **1000-2100**" in md
    assert "No NegativeUNL events exist in the scanned history" in md


def test_main_exits_1_on_corrupted_reconstruction(tmp_path):
    evs = lifecycle()
    evs[5]["nunl"] = evs[5]["next"] = {"disabled": [KB], "to_disable": None, "to_reenable": None}
    p = tmp_path / "h.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in evs) + "\n")
    assert A.main(["--hist", str(p), "--out", str(tmp_path / "o")]) == 1
    assert "Result: **FAIL**" in (tmp_path / "o" / "REPORT.md").read_text()


# ---------- risk windows ----------


def vals_fixture(lo, hi, signers):
    """Ledger stream lo..hi; signers = {master: set(ledgers signed with the right hash)}."""
    evs = [{"ev": "ledger", "li": li, "lh": f"L{li}"} for li in range(lo, hi + 1)]
    for mk, lis in signers.items():
        for li in sorted(lis):
            evs.append({"ev": "val", "k": "s" + mk, "mk": mk, "li": li, "lh": f"L{li}"})
    return evs


def test_risk_scores_and_status():
    w = range(256, 512)  # window ending at voting ledger 511
    signers = {"nA": set(w), "nB": set(list(w)[:127]), "nC": set(list(w)[:128])}
    vals = vals_fixture(200, 515, signers)
    vals.append({"ev": "val", "k": "snC", "mk": "nC", "li": 500, "lh": "WRONG"})  # wrong hash: not counted
    unl = {"keys": ["nA", "nB", "nC"], "signing": {}, "t": "x", "quorum": 2}
    windows, rows, lo, hi = A.risk(vals, unl, listed=set())
    assert windows == [511] and (lo, hi) == (200, 515)
    got = {r["validator"]: (r["score"], r["status"]) for r in rows}
    assert got == {"nA": (256, "OK"), "nB": (127, "DISABLE_CANDIDATE"), "nC": (128, "WATCH")}
    st = A.score_stats(rows)
    assert (st["count"], st["min"], st["median"], st["max"]) == (3, 127, 128, 256)


def test_risk_skips_incomplete_windows_and_maps_signing_keys():
    vals = vals_fixture(250, 767, {})
    vals = [e for e in vals if e.get("li") != 600]  # gap inside window 512..767
    vals += [{"ev": "val", "k": "sigX", "li": li, "lh": f"L{li}"} for li in range(300, 512)]
    unl = {"keys": ["nX"], "signing": {"sigX": "nX"}, "t": "x", "quorum": 1}
    windows, rows, _, _ = A.risk(vals, unl, listed={"nX"})
    assert windows == [511]  # 256..511 complete; 512..767 has a gap at 600
    assert rows[0]["score"] == 212 and rows[0]["status"] == "REENABLE"


# ---------- collector ----------


class FakePost:
    def __init__(self, answers):
        self.answers = list(answers)
        self.bodies = []

    def __call__(self, body):
        self.bodies.append(json.loads(body))
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return {"result": a}


def test_rpc_retries_with_backoff_and_logs(capsys):
    post = FakePost([{"error": "lgrNotFound"}, OSError("reset"), {"status": "success", "x": 1}])
    sleeps = []
    r = C.Rpc("u", gap=0.1, post=post, sleep=sleeps.append)
    assert r.call("ledger", {"ledger_index": 5}) == {"status": "success", "x": 1}
    assert r.retried == 2 and sleeps == [2.0, 0.1, 4.0, 0.1]
    out = capsys.readouterr().out
    assert "RETRY ledger 5 attempt 1/6 error=lgrNotFound backoff=2.0s" in out
    assert "attempt 2/6 error=OSError: reset backoff=4.0s" in out


def test_rpc_gives_up_and_refuses_writes():
    r = C.Rpc("u", retries=2, post=FakePost([{"error": "tooBusy"}] * 2), sleep=lambda s: None)
    with pytest.raises(C.RpcError, match="giving up after 2"):
        r.call("ledger", {})
    with pytest.raises(ValueError):
        C.Rpc("u", post=FakePost([])).call("submit", {})
    with pytest.raises(C.RpcError, match="entryNotFound"):
        C.Rpc("u", post=FakePost([{"error": "entryNotFound"}])).call("ledger_entry", {})
    assert (
        C.Rpc("u", post=FakePost([{"error": "entryNotFound"}])).call("ledger_entry", {}, absent_ok=True)
        is None
    )


def test_fetch_flag_parses_unlmodify_and_object():
    node = {
        "DisabledValidators": [{"DisabledValidator": {"PublicKey": KB, "FirstLedgerSequence": 256}}],
        "ValidatorToDisable": KA,
    }
    tx = {
        "TransactionType": "UNLModify",
        "UNLModifyValidator": KA,
        "UNLModifyDisabling": 1,
        "LedgerSequence": 512,
        "hash": "T",
    }
    post = FakePost(
        [
            {
                "validated": True,
                "ledger": {
                    "ledger_index": "512",
                    "ledger_hash": "H",
                    "close_time": 9,
                    "transactions": [{"TransactionType": "SetFee"}, tx],
                },
            },
            {"node": node},
            {"node": node},
        ]
    )
    e = C.fetch_flag(C.Rpc("u", post=post, sleep=lambda s: None), 512)
    assert e["mods"] == [{"validator": KA, "disabling": 1, "seq": 512, "hash": "T"}]
    assert e["nunl"] == {"disabled": [KB], "to_disable": KA, "to_reenable": None} == e["next"]
    assert [b["params"][0].get("ledger_index") for b in post.bodies] == [512, 512, 513]


def test_fetch_flag_rejects_incomplete_answer():
    post = FakePost([{"validated": False, "ledger": {"ledger_index": "512"}}])
    with pytest.raises(C.RpcError, match="incomplete"):
        C.fetch_flag(C.Rpc("u", post=post, sleep=lambda s: None), 512)


def test_flags_in_range():
    assert C.flags_in(7500054, 7556031)[0] == 7500288 and len(C.flags_in(7500054, 7556031)) == 218
    assert C.flags_in(256, 512) == [256]  # 512 excluded: its F+1 (513) is outside the range
    assert C.flags_in(257, 768) == [512]
    assert C.parse_range("1-5,10-20") == (10, 20)


def test_drop_torn_tail_and_resume_set(tmp_path):
    p = tmp_path / "n.jsonl"
    p.write_text(json.dumps(flag(256)) + "\n" + json.dumps(flag(512)) + "\n" + '{"ev":"fla')
    assert C.drop_torn_tail(str(p)) == len('{"ev":"fla')
    assert C.load_done(str(p)) == ({256, 512}, 0)


# ---------- crosscheck ----------


def test_crosscheck_compare_and_corruption():
    rec = flag(512, [mod(KA, 1, 512)], {"disabled": [], "to_disable": KA, "to_reenable": None})
    loc = {
        "lh": "H512",
        "mods": rec["mods"],
        "nunl": rec["nunl"],
        "next": rec["next"],
        "validated": True,
        "amendments": 64,
    }
    assert X.compare(rec, loc) == []
    assert any(d.startswith("nunl:") for d in X.compare(X.corrupt(rec, 0), loc))
    assert any(d.startswith("lh:") for d in X.compare(X.corrupt(rec, 1), loc))
    no_next = {k: v for k, v in loc.items() if k != "next"}
    assert X.compare(rec, no_next) == []  # F+1 not acquired: not compared
    assert any("positive control" in d for d in X.compare(rec, dict(loc, amendments=None)))


def test_crosscheck_local_refuses_other_commands():
    with pytest.raises(ValueError):
        X.Local("c", runner=lambda b: "{}").call("stop", {})
