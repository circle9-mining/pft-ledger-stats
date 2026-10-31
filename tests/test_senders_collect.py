import csv
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "senders"))
import collect_senders as cs  # noqa: E402

BASE_CT = 815000000  # ripple time


def ledger(idx, txs, ct=None):
    return {
        "ledger_index": str(idx),
        "ledger_hash": f"H{idx}",
        "close_time": ct if ct is not None else BASE_CT + (idx - 100) * 4,
        "closed": True,
        "transactions": txs,
    }


def tx(i, acct="rA", tt="Payment", res="tesSUCCESS", fee="12", seq=5, ticket=False):
    t = {"hash": f"T{i}", "Account": acct, "TransactionType": tt, "Fee": fee, "Sequence": seq,
         "metaData": {"TransactionIndex": i, "TransactionResult": res}}
    if ticket:
        t["TicketSequence"] = 9
        t["Sequence"] = 0
    return t


def test_sender_rows_sorted_and_fields():
    rows = cs.sender_rows(ledger(100, [tx(2, "rB", ticket=True), tx(0), tx(1, res="tecNO_DST", fee="15")]))
    assert [r["tx_index"] for r in rows] == [0, 1, 2]
    assert rows[1]["result"] == "tecNO_DST" and rows[1]["fee_drops"] == 15
    assert rows[2]["account"] == "rB" and rows[2]["ticket"] == "y" and rows[2]["sequence"] == 0
    assert rows[0]["ticket"] == "n" and rows[0]["sequence"] == 5


def test_meta_key_variant_and_missing_index():
    t = tx(0)
    t["meta"] = t.pop("metaData")
    assert cs.sender_rows(ledger(100, [t]))[0]["tx_index"] == 0
    bad = tx(0)
    del bad["metaData"]["TransactionIndex"]
    with pytest.raises(ValueError):
        cs.sender_rows(ledger(100, [bad]))


def test_in_window_boundary():
    end = BASE_CT + cs.RIPPLE_EPOCH
    assert cs.in_window(BASE_CT, end)
    assert not cs.in_window(BASE_CT + 1, end)


class FakeRpc:
    """Serves ledgers 100..last; ledger `fail` errors `fail_times` times; ignores nothing else."""

    def __init__(self, last, fail=None, fail_times=0, kill_after=None, ct_shift=0):
        self.last, self.fail, self.fail_times, self.kill_after = last, fail, fail_times, kill_after
        self.count = 0
        self.ct_shift = ct_shift

    def __call__(self, *_a, **_k):
        return self

    def call(self, method, params=None):
        self.count += 1
        if method == "server_info":
            return {"info": {"validated_ledger": {"seq": self.last}, "build_version": "x",
                             "server_state": "full", "complete_ledgers": f"100-{self.last}"}}
        i = params["ledger_index"]
        if self.kill_after is not None and i > self.kill_after:
            raise KeyboardInterrupt
        if i == self.fail and self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("boom")
        return {"ledger": ledger(i, [tx(k, f"r{i % 3}") for k in range(i % 4)],
                                 ct=BASE_CT + (i - 100) * 4 + self.ct_shift)}


def run(monkeypatch, tmp_path, fake, *args):
    monkeypatch.setattr(cs, "Rpc", fake)
    monkeypatch.setattr(cs.time, "sleep", lambda s: None)
    monkeypatch.setattr(cs, "_stop", False)
    return cs.main(["--out", str(tmp_path), "--no-sudo", *args])


def read(path):
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def test_full_run_retries_and_gaps(monkeypatch, tmp_path):
    run(monkeypatch, tmp_path, FakeRpc(110, fail=104, fail_times=2), "--start", "100", "--stop-at", "110")
    led = read(tmp_path / "ledgers.csv")
    assert [int(r["ledger_index"]) for r in led] == list(range(100, 111))
    assert len(read(tmp_path / "tx.csv")) == sum(i % 4 for i in range(100, 111))
    assert [r["attempt"] for r in read(tmp_path / "retries.csv")] == ["1", "2"]
    assert not (tmp_path / "gaps.csv").exists()
    g = tmp_path / "g"
    run(monkeypatch, g, FakeRpc(106, fail=103, fail_times=9), "--start", "100", "--stop-at", "106")
    assert [r["ledger_index"] for r in read(g / "gaps.csv")] == ["103"]
    assert "103" not in [r["ledger_index"] for r in read(g / "ledgers.csv")]


def test_resume_after_kill_with_orphans_and_torn_line(monkeypatch, tmp_path):
    with pytest.raises(KeyboardInterrupt):
        run(monkeypatch, tmp_path, FakeRpc(115, kill_after=107), "--start", "100", "--stop-at", "115")
    # simulate a kill between the tx rows and the ledger row of 108, plus a torn ledger row
    cs.append_rows(tmp_path / "tx.csv", cs.TX_FIELDS, cs.sender_rows(ledger(108, [tx(0), tx(1)])))
    with open(tmp_path / "ledgers.csv", "a") as fh:
        fh.write("108,H108,8150")
    run(monkeypatch, tmp_path, FakeRpc(115), "--start", "999", "--stop-at", "999")
    led = [int(r["ledger_index"]) for r in read(tmp_path / "ledgers.csv")]
    assert led == list(range(100, 116))
    txs = read(tmp_path / "tx.csv")
    assert len(txs) == sum(i % 4 for i in range(100, 116))
    assert len({(r["ledger_index"], r["tx_index"]) for r in txs}) == len(txs)
    rs = read(tmp_path / "restarts.csv")
    assert [r["kind"] for r in rs] == ["start", "resume"]
    assert rs[1]["first_ledger"] == "108" and rs[1]["orphan_rows"] == "2" and int(rs[1]["torn_bytes"]) > 0
    st = json.loads((tmp_path / "state.json").read_text())
    assert st["start_seq"] == 100 and st["stop_at"] == 115


def test_window_end_enforced_client_side(monkeypatch, tmp_path):
    monkeypatch.setattr(cs.time, "time", lambda: BASE_CT + cs.RIPPLE_EPOCH - 3600 + 20)
    # 1h window ending at BASE_CT+20 (ripple): ledgers 100..105 close at +0..+20, 106 at +24
    run(monkeypatch, tmp_path, FakeRpc(120), "--start", "100", "--hours", "1", "--stop-at", "120")
    led = [int(r["ledger_index"]) for r in read(tmp_path / "ledgers.csv")]
    assert led == list(range(100, 106))


def test_rate_cap(monkeypatch, tmp_path):
    with pytest.raises(SystemExit):
        run(monkeypatch, tmp_path, FakeRpc(101), "--rate", "2")
