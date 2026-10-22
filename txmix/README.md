# txmix — what Post Fiat testnet transactions actually are

Three scripts that record every transaction in a window of validated ledgers on your own node,
then report the transaction-type mix, result codes, fees, sender concentration and memo use.

| script | role | dependencies |
|---|---|---|
| `collect_tx.py` | throttled, resumable forward collector (one CSV row per transaction) | Python 3 stdlib |
| `analyze_tx.py` | `REPORT.md` + 3 PNG charts from a collected window | stdlib + `matplotlib` (charts) |
| `crosscheck_tx.py` | re-fetches random collected ledgers and compares them with the CSVs | Python 3 stdlib |

## Run it

```bash
cd pft-ledger-stats

# 24h window from the first start, <= 1 request/second, sudo docker exec into "postfiatd"
python3 txmix/collect_tx.py --out data/txmix --log data/txmix/collect.log --hours 24
#   --container NAME      container name (default postfiatd)
#   --port N              admin JSON-RPC port inside the container (default 5005)
#   --no-sudo             if your user is in the docker group
#   --rate R              requests/second, at most 1 (higher is refused)
#   --start SEQ           first ledger (default: current validated ledger)
#   --sample-interval S   server_info sample period (default 600)

# while the ledgers are still in node history (see Limits): 5 random ledgers vs fresh RPC
python3 txmix/crosscheck_tx.py --data data/txmix --count 5

# report
python3 -m venv .venv && .venv/bin/pip install matplotlib pytest ruff
.venv/bin/python txmix/analyze_tx.py --data data/txmix --out txmix/report

# tests + lint
.venv/bin/python -m pytest -q tests/test_txmix.py && .venv/bin/ruff check txmix tests
```

For an unattended 24h run, wrap the collector in a systemd service with `Restart=on-failure`. A
restart resumes at the ledger after the last row of `ledgers.csv`/`gaps.csv`, and the window end
stays fixed by `state.json`.

## Method

**Collector.** Only `server_info` and `ledger` may be sent (any other method raises). Requests go
through `docker exec <container> curl http://127.0.0.1:<port>/`, so the admin port is never
exposed. A rate limiter keeps at least 1 s between requests. Each validated ledger is fetched once
with `ledger {ledger_index, transactions: true, expand: true}`. A ledger that still fails after
`--retries` attempts goes to `gaps.csv` and is never interpolated. Every `--sample-interval`
seconds a `server_info` sample (validated seq, base fee, reserve, load factor, state) is appended
to `samples.jsonl`.

Per transaction `tx.csv` holds: `ledger_index`, `close_time` (+ ISO), `tx_index`, `tx_hash`,
`tx_type`, `result` (`TransactionResult` from metadata), `fee_drops`, `account`, `memo` (y/n),
`signers` (multisig signer count), `delivered_class` (`none`/`native`/`issued`/`unavailable`) and
`accounts_created` (`CreatedNode` entries of type `AccountRoot` in the metadata, i.e. newly funded
accounts).

**Account hashing.** Addresses are never written in clear. `account` is the first 16 hex chars of
HMAC-SHA256(key, address), with a random 32-byte key created once in `<out>/.hmac_key` (mode
0600, git-ignored, never published). Within one run the same address always maps to the same
token, which is enough to count senders and measure concentration. Without the key, the tokens
cannot be linked back to addresses or matched across runs.

**Analyzer.** Rows are de-duplicated by `tx_hash` before counting. If the collector crashes
between the `tx.csv` and `ledgers.csv` appends, a ledger's rows can repeat on resume, and the
number dropped is printed in the report. Fee percentiles use nearest rank (the p-th percentile is
the value at rank ceil(p/100·n)), so every reported fee is one that was actually paid. They are
compared with the base fee taken from the `server_info` samples (10 drops if there are none).
Gini is computed over transactions per sender with G = Σ(2i − n − 1)·xᵢ / (n·Σx), i ascending. The
report also gives the top-10 sender share and a Lorenz chart.

**Cross-check.** `crosscheck_tx.py` picks random collected ledgers that the node still holds,
re-fetches them and compares `ledger_hash`, `txn_count`, the number of `tx.csv` rows and the set
of tx hashes. Exit codes: 0 all match, 1 any mismatch, 2 too few collected ledgers still in
history.

**Tests.** `tests/test_txmix.py` runs a synthetic positive control: a hand-built window with a
known type mix, result classes, fees (nearest-rank percentiles, share above 10× base), sender
counts (known Gini and top-10 share), memos, multisig and created accounts. It also checks the
collector's metadata classifiers, de-duplication and that a `REPORT.md` is written end to end.

## Limits

- **Single node's view, testnet only.** The data is what one node validated. It describes Post
  Fiat testnet traffic, which is dominated by test and automation accounts, and says nothing about
  mainnet.
- **Short history on light nodes.** The `testnet-light` image ships with `[node_db]
  online_delete=512`, so the node keeps only about 512–1,024 recent ledgers (under an hour). If
  the collector is down longer than that, the missing ledgers cannot be fetched and are listed in
  `gaps.csv`. The cross-check can only sample recent ledgers, so it has to run during or right
  after the window.
- **Hashed accounts.** "Active accounts" means distinct transaction senders (`Account`). It does
  not count destinations or other participants. Tokens are only comparable within one run.
- **Fees are what was paid**, including the fee escalation applied under load. They are not what
  each sender would have needed to pay.
- `delivered_class` tells native PFT apart from issued amounts. It does not record amounts.

## Privacy

Published outputs (`REPORT.md`, charts, the gzipped `tx.csv`, logs) contain only public chain
data (ledger indices and hashes, tx hashes, types, results, fees) and HMAC account tokens. The
HMAC key, node keys, peer addresses and host names are never written. Published logs start with
the OS line and contain no IPs.
