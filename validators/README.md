# validators — per-validator participation on the Post Fiat testnet

The top-level `pft-ledger-stats` report counts proposers per ledger. This folder goes one level
down: for every validator whose validations reach your node it reports how often it signed the
canonical ledger, its longest run of missed ledgers, validations for a different (non-canonical)
hash, signing delay, UNL vs non-UNL split, trusted validations per ledger vs quorum, and churn.

| script | role | dependencies |
|---|---|---|
| `collect_validations.py` | resumable 24h recorder of the `validations` + `ledger` streams, gap logging | Python 3 stdlib |
| `analyze_validations.py` | `REPORT.md`, `validators_summary.csv`, `ledger_quorum.csv`, 3 PNG charts | stdlib + `matplotlib` (charts only) |
| `crosscheck_validations.py` | re-fetches random collected ledgers with `ledger` and compares them | Python 3 stdlib |

## Run it

```bash
cd pft-ledger-stats

# 24h window, fixed from the FIRST start (window.json), sudo docker exec into "postfiatd"
python3 validators/collect_validations.py --out data/validators --hours 24
#   --container NAME   container name (default postfiatd)
#   --port N           admin WebSocket port inside the container (default 6006)
#   --no-sudo          if your user is in the docker group

# right after the window (see Limits): 5 random collected ledgers vs a fresh `ledger` call
python3 validators/crosscheck_validations.py --data data/validators --count 5 --seed 20261023

# report
python3 -m venv .venv && .venv/bin/pip install matplotlib pytest ruff
.venv/bin/python validators/analyze_validations.py --data data/validators --out validators/report

# tests + lint
.venv/bin/python -m pytest -v -s tests/test_validators.py && .venv/bin/ruff check validators tests
```

For an unattended run, wrap the collector in a systemd service with `Restart=on-failure`
(the published run used `User=` an unprivileged account, `WorkingDirectory=` the repo).
A restart reconnects, keeps the same window end and writes any ledgers it did not see to
`gaps.csv`.

## Method

**Collector.** The node's admin WebSocket stays on loopback inside the container. The collector
runs its own small WebSocket client inside the container (`docker exec -i <container> timeout <s>
python3 -I -c <this file> relay ...`) and exchanges JSON lines with it over the exec pipe. Only
`subscribe`, `server_info`, `validators` and `manifest` are ever sent; the allowlist is enforced
on both sides of the pipe. Per message it appends:

- `validations.csv`: `ledger_index, ledger_hash, master_key, master_source, signing_key,
  signing_time, signing_time_iso, recv_utc, full`. The master key comes from the stream's
  `master_key` field, or from a `manifest` lookup of the signing key; `master_source` records
  which (`stream`, `manifest`, or `unresolved` if the lookup failed). Published run: 1,695,630
  `stream`, 28,550 `manifest`, 0 `unresolved`.
- `ledgers.csv`: one row per `ledgerClosed` message (the node's validated ledger stream):
  `ledger_index, ledger_hash, ledger_time, ledger_time_iso, txn_count, recv_utc`.
- `manifests.csv`: master key → ephemeral key, sequence, manifest **domain**.
- `snapshots.jsonl`: `server_info` (build, state, quorum, validated seq) and `validators`
  (trusted keys, quorum, publisher list seq/expiry) at start and hourly.
- `gaps.csv`: a jump in the ledger stream's index (disconnect, restart) is written as a gap range,
  never filled in silently.
- `collect.log`: `WINDOW` line, connects, hourly `progress` counters, `END` line with row counts.

The relay runs under `timeout <seconds left in window>`, so at the window end the log shows
`relay exited rc=124` followed by the `END` summary.

**Analyzer.** "Observed ledgers" are the rows of `ledgers.csv`; ledgers in `gaps.csv` are left out
of every denominator. For each master key:

| statistic | definition |
|---|---|
| participation % | ledgers with a validation for the canonical hash / observed ledgers in the window |
| active % | same, but only over observed ledgers between the key's first and last validation seen |
| missed, longest streak | observed ledgers with no canonical validation; longest consecutive run of them |
| non-canonical | ledgers where the key validated a different hash and never the canonical one |
| sign delay | median `signing_time` − ledger close time (seconds, 1 s resolution) |
| recv lag | median arrival time at this node − `signing_time` (relay latency + clock skew) |
| UNL | key is in the node's `trusted_validator_keys` (last `validators` snapshot) |

Per ledger it counts trusted keys that signed the canonical hash and compares that count with
`validation_quorum`. Churn lists keys first seen, or last seen, more than 100 ledgers inside the
window, trusted keys never seen, and UNL additions/removals between the first and last snapshot.
Validators are shown only as public master keys and manifest domains.

The *active %* column was added after the first real run. One validator joined about 16,700
ledgers into the window and showed 41.32 % participation, which is correct over the full window
but says nothing about how it behaved once it existed. Its active % is 100.0. Both columns are
kept: participation % is the card's statistic, active % separates churn from missed ledgers.

## Tests

`tests/test_validators.py` builds synthetic fixtures for every statistic. Positive control: over
100 ledgers, validator B misses ledgers 41–50 and must report **90.0 %** participation, 10 missed,
a **10-ledger** longest streak (the test prints that line under `-s`). Other cases: dedupe of
repeated validations, a validator that disagrees on one ledger and leaves at ledger 50, a trusted
key that never appears, quorum shortfall per ledger, signing delay/receive lag, UNL split and
order, UNL changes between snapshots, churn margins, a late joiner's active %, the rendered
Markdown table row and the cross-check comparison (every field mismatch must fail).

`TestResume` runs two collector sessions on one `--out` directory without a node: a torn half
row is left after the first (as a kill during a flush would leave it), and the second session
gets a replay of the last ledger and then a 2-ledger jump. It asserts the first session's rows
are an unchanged prefix, the replayed ledger is not written twice, the torn row is dropped
(`RESUME dropped torn last line ...` in the log), and the gap is written to `gaps.csv` with the reason
`reconnect/restart`. The first run of this test found a labelling bug, now fixed: a replayed
ledger reset the reconnect flag, so the gap was labelled `skipped in stream`.

### Resume drill (live node)

The published 24h run had no restart (0 gaps), so it doesn't exercise resume.
`resume_drill.sh <new-dir>` does: it runs a real 8-minute window on the local node with the
same allowlisted commands, sends SIGTERM after 120 s, restarts after 30 s down, sends SIGKILL
90 s later, restarts again and lets the window finish. `resume_check.py` then checks:
1 `WINDOW` + 2 `RESUME` lines for the same window; each CSV as saved at each interrupt is a
byte-identical prefix of the final file; every line has the right column count; no duplicate
`(ledger_index, ledger_hash, signing_key)` and no duplicate ledger; and every missing ledger
falls in a `gaps.csv` range. Logs: `logs/validators/resume-drill-1.log` (collector before the
two fixes above) and `resume-drill-2.log` (final code). Ledgers that close while the collector
is down are **not** backfilled: the streams carry no history. They are recorded as
`reconnect/restart` gaps, and the analyzer leaves them out of the observed set.

## Limits

- **One node's view.** The node only sees validations that are relayed to it. A missed ledger
  means no validation for the canonical hash *reached this node*; the cause can be the validator,
  its peering, or this node's connectivity. It is not proof of a validator fault.
- **UNL is this node's UNL** (the default publisher list it loaded), taken from the last snapshot.
- **History is short.** The testnet-light image runs `online_delete=512`, so the node holds only
  the most recent ~500–1,000 ledgers. The cross-check has to run right after the window and can
  only sample the last few hundred collected ledgers; older window ledgers can't be re-fetched
  from this node.
- Sign delay has 1 s resolution: ledger close time and `signing_time` are whole seconds.
- Validations that arrive for ledgers outside the observed set (before the first or after the
  last `ledgerClosed`) are ignored.

## Published run (2026-10-22T14:20:11Z – 2026-10-23T14:20:11Z)

One 24h window on a testnet-light node (postfiatd 1.0.9, `server_state: full`, 10 peers,
Ubuntu 22.04.5). The collector ran from start to end without a restart: **28,550 ledgers**,
**1,724,180 validation rows**, **61 manifests**, **0 gaps**.

| file | contents |
|---|---|
| `../logs/validators/collect.log` | collector run log: `WINDOW` start/end, hourly counters, `END` row counts, gaps |
| `../logs/validators/crosscheck.log` | 5 random ledgers vs fresh `ledger` RPC, run at 14:20:24Z right after the window |
| `../logs/validators/tests-1.log`, `tests-2.log` | test run before and after the active % change (`-s` prints the positive control) |
| `../logs/validators/analyze-1-failed.log` | first analyzer run: system Python has no matplotlib, kept as it happened |
| `../logs/validators/analyze-2.log`, `analyze-3.log` | analyzer with the repo venv, before and after the active % change |
| `../logs/validators/reproduce.log` | the reproduce steps below, run on the committed `.gz` data: identical CSVs |
| `report/REPORT.md` + `report/*.png` + `report/*.csv` | the final report from `analyze-3.log` |
| `../data/validators/*.csv.gz`, `snapshots.jsonl.gz`, `window.json` | the collected data (gzip -9) |

In published logs the local username and home path are replaced with `<user>` and `~`.

**Cross-check.** `crosscheck_validations.py` picked 5 random ledgers (seed 20261023) from the 794
collected ledgers the node still held (complete range 7414764–7415562). For each one it compares
`ledger_hash`, close time and transaction count from `ledgers.csv` with a fresh `ledger` call and
counts `validations.csv` rows for the fresh hash. Result: 5/5 match, 61 canonical validations
each (= every validator seen at that point). There were no differences to explain. All 5 sampled
ledgers are from the last ~800 of the window, because of the retention limit above.

### Reproduce the report from committed data

```bash
mkdir -p /tmp/vdata && cp data/validators/window.json /tmp/vdata/
for f in data/validators/*.gz; do gunzip -c "$f" > /tmp/vdata/"$(basename "${f%.gz}")"; done
.venv/bin/python validators/analyze_validations.py --data /tmp/vdata --out /tmp/vreport
diff <(sed 1d validators/report/validators_summary.csv) <(sed 1d /tmp/vreport/validators_summary.csv) && echo same
```
