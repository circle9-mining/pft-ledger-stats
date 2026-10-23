# txmix — what Post Fiat testnet transactions actually are

Three scripts that record every transaction in a window of validated ledgers on your own node,
then report the transaction-type mix, result codes, fees, sender concentration and memo use.

| script | role | dependencies |
|---|---|---|
| `collect_tx.py` | throttled, resumable forward collector (one CSV row per transaction) | Python 3 stdlib |
| `analyze_tx.py` | `REPORT.md` + 3 PNG charts from a collected window | stdlib + `matplotlib` (charts) |
| `crosscheck_tx.py` | re-fetches random collected ledgers and compares them with the CSVs | Python 3 stdlib |
| `resume_drill.sh` + `resume_check.py` | live SIGTERM/SIGKILL interrupt-and-restart drill with checks | bash, Python 3 stdlib |
| `reproduce.sh` | rebuilds `report/REPORT.md` from the published `data/txmix/*.gz` and compares it byte-for-byte (sha256 + `cmp`) | bash + `.venv` |

## Results: 24h run 2026-10-22T17:14:22Z → 2026-10-23T17:14:22Z

Full report: [`report/REPORT.md`](report/REPORT.md). Data: `data/txmix/{tx,ledgers}.csv.gz`,
`samples.jsonl.gz`, `window.json`. Logs: [`logs/txmix/`](../logs/txmix/).

- **28,561 ledgers** (7390454–7419014), **0 gaps**, 80,355 requests (≤ 1/s). 81.2% of ledgers were empty.
- **9,890 transactions** (0.35 per ledger), 0 duplicate rows. Only 3 types were seen: Payment 79.4%,
  NFTokenMint 11.4%, NFTokenBurn 9.2%.
- **Results:** 6 non-tesSUCCESS (all `tecUNFUNDED_PAYMENT` on Payments, 0.1%).
- **Fees:** p50 = p90 = p99 = 10 drops (the base fee), max 12. 99.6% of transactions paid exactly the
  base fee and none paid more than 10× it. In total 98,986 drops (0.099 PFT) were burned.
- **Senders:** 46 active accounts, top-10 share 33.0%, Gini 0.266. The busiest sender sent 335 tx and
  the median sender 245, and no account sent only one. Traffic is spread evenly over a fixed set of
  senders, which looks like scheduled automation rather than organic use.
- 14 accounts were newly funded (all by Payment). 43 transactions (0.4%) carried memos, and none
  were multi-signed.
- The hourly rate was flat at about 390–445 tx/h. The first and last bars of `types_by_hour.png`
  are partial hours (46 and 14 minutes).

Checks for this run: the live cross-check matched 5/5 random collected ledgers against fresh `ledger`
RPC (hash, txn_count, tx rows, tx-hash set). The negative control, a copy with every ledger hash
altered, failed 0/5 with exit 1. `reproduce.sh` rebuilt a byte-identical `REPORT.md` (same sha256, `cmp` exit 0) from the `.gz`
files (`logs/txmix/reproduce-2.log`; the first run, `reproduce.log`, compared with chart link lines excluded).

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

# rebuild the published report from the published .gz data
txmix/reproduce.sh
```

For an unattended 24h run, wrap the collector in a systemd service with `Restart=on-failure`. A
restart resumes at the ledger after the last row of `ledgers.csv`/`gaps.csv`, and the window end
stays fixed by `state.json`. Before resuming, the collector repairs what a kill can leave behind and
logs each repair as a `RESUME dropped ...` line:
- a torn (partial) last line in any CSV is cut;
- `tx.csv` rows of a ledger whose `ledgers.csv` row was never written are cut. A ledger's tx rows
  are appended before its ledger row, so without this cut they would be written twice.

A restart before the first ledger row keeps the original window. `resume_drill.sh <new dir>` proves
this live: it runs an 8-minute window (`MINUTES=`), sends SIGTERM after 120 s, restarts after 30 s,
sends SIGKILL 90 s later, restarts again, and then checks that the window was kept, that each
interrupt snapshot is a byte-identical prefix of the final files, and that there are no duplicates,
torn rows, unaccounted gaps or curl processes left in the container. In the published drill
(`logs/txmix/resume-drill.log`, 9/9 PASS) the SIGKILL landed between ledgers, so it left no torn line
and no orphan rows. That repair path is covered by `TestResume` instead.

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
history. Right after the 24h window, the node listed its oldest ledger in `complete_ledgers` but
answered `lgrNotFound` for it, because online_delete was removing it. That ledger crashed the first
negative-control run (`logs/txmix/crosscheck-negative.log`). Since then a ledger that is listed but
not served is printed as `SKIP` and replaced by another random ledger. It never counts as a match,
and if too few ledgers can be fetched the exit code is 2.

**Tests.** `tests/test_txmix.py` runs a synthetic positive control: a hand-built window with a
known type mix, result classes, fees (nearest-rank percentiles, share above 10× base), sender
counts (known Gini and top-10 share), memos, multisig and created accounts. It also checks the
collector's metadata classifiers, de-duplication and that a `REPORT.md` is written end to end.
`TestResume` drives the real `collect_tx.main()` against a fake RPC. It covers a kill between the
tx and ledger appends with torn lines in both files, and a restart before the first ledger row.
`TestCrosscheckSkip` covers the `lgrNotFound` skip and the exit-2 path.

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
