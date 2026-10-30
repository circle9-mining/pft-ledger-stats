import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "keyposture"))
import collect_key as C  # noqa: E402


class FakeRpc:
    """Serves synthetic ledgers 100..; ledger 103 and 105 carry key txs. stop_after sets _stop."""

    def __init__(self, *a, stop_after=None, **k):
        self.count = 0
        self.stop_after = stop_after

    def call(self, method, params=None):
        self.count += 1
        if method == "server_info":
            return {
                "info": {
                    "build_version": "t",
                    "server_state": "full",
                    "complete_ledgers": "1-200",
                    "validated_ledger": {"seq": 110},
                }
            }
        i = params["ledger_index"]
        if self.stop_after is not None and i >= self.stop_after:
            C._stop = True
        txs = [
            {
                "TransactionType": "Payment",
                "hash": f"P{i}",
                "metaData": {"TransactionIndex": 0, "AffectedNodes": []},
            }
        ]
        if i in (103, 105):
            txs.append(
                {
                    "TransactionType": "SetRegularKey",
                    "hash": f"K{i}",
                    "metaData": {"TransactionIndex": 1, "AffectedNodes": []},
                }
            )
        return {
            "ledger": {
                "ledger_index": str(i),
                "ledger_hash": f"H{i}",
                "close_time": 800000000 + i,
                "closed": True,
                "transactions": txs,
            }
        }


def run(monkeypatch, out, stop_after=None):
    monkeypatch.setattr(C, "Rpc", lambda *a, **k: FakeRpc(stop_after=stop_after))
    C._stop = False
    return C.main(["--out", str(out), "--start", "100", "--stop-at", "107", "--no-sudo"])


def strip(path):
    return [line.rsplit(",", 1)[0] for line in open(path).read().splitlines()]


def test_resume_after_stop_torn_and_orphan(monkeypatch, tmp_path):
    ref, drl = tmp_path / "ref", tmp_path / "drl"
    assert run(monkeypatch, ref) == 0
    assert run(monkeypatch, drl, stop_after=103) == 0  # SIGTERM-like stop after ledger 103
    rows = strip(drl / "ledgers.csv")
    assert rows[-1].startswith("103,")
    # SIGKILL-like damage: an orphan jsonl line for ledger 104 (no ledgers.csv row) + torn tails
    with open(drl / "key-tx.jsonl", "a") as fh:
        fh.write(json.dumps({"ledger_index": 104, "tx": {"hash": "X"}}) + "\n")
        fh.write('{"ledger_index": 104, "tx"')
    with open(drl / "ledgers.csv", "a") as fh:
        fh.write("104,H104,8000")
    assert run(monkeypatch, drl) == 0
    assert (ref / "key-tx.jsonl").read_bytes() == (drl / "key-tx.jsonl").read_bytes()
    assert strip(ref / "ledgers.csv") == strip(drl / "ledgers.csv")
    assert [json.loads(x)["tx"]["hash"] for x in open(drl / "key-tx.jsonl")] == ["K103", "K105"]
    assert not (drl / "gaps.csv").exists()
