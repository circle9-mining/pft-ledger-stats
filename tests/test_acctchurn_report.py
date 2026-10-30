import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "acctchurn"))
import diff_churn  # noqa: E402
import report_churn  # noqa: E402
from test_acctchurn_diff import acct, write  # noqa: E402

FULL = [
    "rOldAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "rNewBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
    "rSameCCCCCCCCCCCCCCCCCCCCCCCCCCCC",
]
FUND = {
    "created": [],
    "funders": [],
    "chains": [],
    "not_found": [],
    "never_active": 0,
    "rpc": {"calls": 3, "retries": 2, "retries_by_error": {"internal": 1, "lgrNotFound": 1}},
}


def doc(tmp_path):
    pa = write(tmp_path / "a.jsonl.gz", 100, 1500, [acct(1, FULL[0], 1_000), acct(3, FULL[2], 500)])
    pb = write(
        tmp_path / "b.jsonl.gz",
        200,
        1488,
        [acct(1, FULL[0], 448), acct(2, FULL[1], 540), dict(acct(3, FULL[2], 500), OwnerCount=2)],
    )
    ha, a = diff_churn.load(pa)
    hb, b = diff_churn.load(pb)
    return json.loads(json.dumps(diff_churn.diff(ha, a, hb, b, fees_sum=12)))


def test_balance_section_retries_and_crosscheck_lines(tmp_path):
    d = doc(tmp_path)
    xc = tmp_path / "xc.log"
    xc.write_text("$ cmd\nOK sample x\nSUMMARY checked=7 mismatches=0 negative=False\n(exit 0)\n")
    text = report_churn.render(d, FUND, crosscheck=report_churn.summaries(xc), tests="9 passed")
    assert "| gross increase | 0.000540 | 1 |" in text
    assert "| gross decrease | -0.000552 | 1 |" in text
    assert "| net | -0.000012 | 2 |" in text
    assert "| changed, same balance | - | 1 |" in text
    assert "| `rOldAAAA…` | -0.000552 | no |" in text and "| `rNewBBBB…` | 0.000540 | yes |" in text
    assert "2 retries (by error: internal 1, lgrNotFound 1)" in text
    assert "- crosscheck: `SUMMARY checked=7 mismatches=0 negative=False`" in text
    assert "- tests: 9 passed" in text
    for addr in FULL:
        assert addr not in text


def test_chart_is_written_and_deterministic(tmp_path):
    d = doc(tmp_path)
    out, f = tmp_path / "REPORT.md", tmp_path / "f.json"
    f.write_text(json.dumps(FUND))
    dj = tmp_path / "d.json"
    dj.write_text(json.dumps(d))
    args = [str(dj), str(f), "--out", str(out), "--chart", str(tmp_path / "c.png")]
    assert report_churn.main(args) == 0
    first = (tmp_path / "c.png").read_bytes()
    assert first[:8] == b"\x89PNG\r\n\x1a\n"
    assert "![Per-account balance change](c.png)" in out.read_text()
    assert report_churn.main(args) == 0
    assert (tmp_path / "c.png").read_bytes() == first
