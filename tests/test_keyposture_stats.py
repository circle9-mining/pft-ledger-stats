import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "keyposture"))
sys.path.insert(0, os.path.dirname(__file__))
import reconcile_key as R  # noqa: E402
import stats_key as S  # noqa: E402
from test_keyposture_reconcile import A0, A1, A2, LINES, SNAP_A, SNAP_B, write_snap, write_tx  # noqa: E402


@pytest.fixture
def inputs(tmp_path):
    a = write_snap(tmp_path / "a.gz", 10, SNAP_A)
    b = write_snap(tmp_path / "b.gz", 15, SNAP_B)
    t = write_tx(tmp_path / "tx", 10, 15, LINES)
    res = R.reconcile(a, b, t)
    rf = tmp_path / "rf.jsonl"
    rf.write_text(
        json.dumps({"account": A0, "kind": "range"})
        + "\n"
        + json.dumps({"kind": "tx", "direction": "out", "destination": A2})
        + "\n"
        + json.dumps({"kind": "tx", "direction": "in", "destination": A0})
        + "\n"
        + json.dumps({"kind": "tx", "direction": "out", "destination": "rGone"})
        + "\n"
    )
    return res, a, b, str(rf)


def test_class_tables_and_transitions(inputs):
    res, a, b, rf = inputs
    s = S.compute(res, a, b, rf)
    assert (
        s["classes_a"]["master_only"]["accounts"] == 3
        and s["classes_a"]["master+regular+multisig"]["accounts"] == 1
    )
    assert s["classes_b"]["regular_only"] == {"accounts": 1, "share": 0.25, "pft": 0.0001, "pft_share": 0.25}
    assert s["created"] == 1 and s["deleted"] == 1
    assert s["weak_b"]["master_still_enabled"]["n"] == 2
    assert s["weak_b"]["single_signer_meets_quorum"]["accounts"] == [A1]
    roles = {(r["role"], r["class_b"]) for r in s["rewardflow"]}
    assert roles == {("payer", "master+regular"), ("recipient", "regular_only"), ("recipient", "absent")}
    assert sum(sum(v for k, v in r.items() if k != "bucket") for r in s["buckets_b"]) == 4


def test_refuses_failed_negative_and_mismatched(inputs):
    res, a, b, _ = inputs
    with pytest.raises(ValueError):
        S.compute(dict(res, result="FAIL"), a, b)
    with pytest.raises(ValueError):
        S.compute(dict(res, negative="x"), a, b)
    with pytest.raises(ValueError, match="do not match"):
        S.compute(res, b, b)


def test_main_deterministic_json_md_chart(inputs, tmp_path):
    res, a, b, rf = inputs
    rj = tmp_path / "r.json"
    rj.write_text(json.dumps(res))
    outs = []
    for i in (1, 2):
        j = tmp_path / f"s{i}.json"
        assert (
            S.main(
                [
                    "--reconcile",
                    str(rj),
                    "--snap-a",
                    a,
                    "--snap-b",
                    b,
                    "--rewardflow",
                    rf,
                    "--json",
                    str(j),
                    "--md",
                    str(tmp_path / "s.md"),
                    "--chart",
                    str(tmp_path / "c.png"),
                ]
            )
            == 0
        )
        outs.append(j.read_bytes())
    assert outs[0] == outs[1]
    assert (tmp_path / "c.png").stat().st_size > 1000
    md = (tmp_path / "s.md").read_text()
    assert "| regular_only | 0 | 0.00% | 1 | 25.00% |" in md
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(dict(res, result="FAIL")))
    assert S.main(["--reconcile", str(bad), "--snap-a", a, "--snap-b", b]) == 2
