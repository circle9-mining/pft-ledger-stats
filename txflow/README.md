# txflow: transaction lifecycle, proposed -> validated

`txmix/` counts what got **into** validated ledgers. It cannot see how long a transaction waited, or
what never made it. `txflow/` watches the node's live streams and measures, for every transaction
hash this node relays:

- **latency** from the first `transactions_proposed` sighting to the validated `transactions` message,
  in seconds and in ledgers (p50/p90/p99/max, overall and per transaction type);
- **never-validated** share within N ledgers (default 20), broken down by the preliminary
  `engine_result` class (`tes`, `ter`, `tel`, `tec`, `tef`, `tem`);
- **fee and queue** behaviour: one `fee` sample per validated ledger (queue size, open-ledger fee,
  expected ledger size) and the periods where the open-ledger fee escalated above the base fee.

It is read-only: the collector sends only `subscribe` and `fee` (an allowlist in the code). Nothing
is submitted or signed. The only external traffic is the optional 20-hash crosscheck against the
public RPC.

## Files

| file | what |
|---|---|
| `collect_flow.py` | resumable stream collector -> `flow.jsonl` (append-only JSON lines) + `state.json` (window) |
| `wsclient.py` | minimal stdlib RFC 6455 client (ws:// only, for the loopback port) |
| `run-collector.sh` | runs the collector inside the container's **network namespace** as an unprivileged user (see below) |
| `analyze_flow.py` | latency / outcome / fee-queue report -> `REPORT.md`, `summary.json`, charts |
| `crosscheck_flow.py` | N random validated hashes vs a public RPC `tx` call (validated, ledger_index, result) |
| `resume_drill.sh`, `resume_check.py` | live SIGTERM + SIGKILL resume drill and its checks |

Tests: `tests/test_txflow_collect.py`, `tests/test_txflow_analyze.py`, `tests/test_txflow_crosscheck.py`.

## Usage

```sh
# 24h collection (systemd unit runs the same command; Restart=on-failure)
sudo RUN_UID=$(id -u) RUN_GID=$(id -g) bash txflow/run-collector.sh \
  --out data/txflow --log data/txflow/collect.log --hours 24
# report + charts
.venv/bin/python txflow/analyze_flow.py data/txflow/flow.jsonl --report txflow/report --charts
# crosscheck 20 random validated hashes against the public RPC (>= 1 s between requests)
python3 txflow/crosscheck_flow.py data/txflow/flow.jsonl --count 20
# tests + lint
.venv/bin/python -m pytest -q tests/test_txflow_*.py && .venv/bin/ruff check txflow tests
# resume drill (separate output dir; 8-minute window)
bash txflow/resume_drill.sh /tmp/txflow-drill
```

### Why `nsenter`

The admin WebSocket (`[port_ws_admin_local]`, 6006) listens on the **container's own** 127.0.0.1.
Docker publishes it on the host's 127.0.0.1:6006, but docker-proxy connects to the container's
bridge address, which the port does not listen on, so a host-side client gets a connection reset.
`run-collector.sh` enters only the container's network namespace (`nsenter -t <pid> -n`), then drops
to the given uid/gid and runs the host's `python3`. Nothing is installed in the container and no
port is opened. If the container restarts, its old namespace stays empty: the collector exits
after 3 failed reconnects and systemd starts it again in the new namespace.

## Data model and gap accounting

`flow.jsonl` events: `session` (each start: resumed?, last ledger, torn bytes cut, bad lines),
`ledger` (each validated ledger from the `ledger` stream), `fee` (the sample taken right after it),
`proposed` (the **first** sighting of a hash with `validated: false`), `validated` (the first
validated message for a hash; validated transactions arrive on both streams and are de-duplicated),
`gap` and `stop`.

A `gap` event records every ledger range the `ledger` stream did not deliver, with its reason:
`stream_skip` (same connection), `reconnect` (the WebSocket dropped) or `restart` (the collector was
down). Gaps are never interpolated. On restart the collector cuts a torn last line (a kill during
a write), rebuilds the seen-hash sets from the file, skips the overlap, and resumes the same window
from `state.json`.

Outcome of each proposed hash, judged at `ledger_current_index + N`: `validated` (within N),
`late` (validated after more than N), `never` (no validated message, and every ledger in the range
observed), `unknown_gap` (a gap overlaps the range, so a validation may have been missed) and
`pending_end` (the data ends too early). Never-validated share = never / (validated + late + never).

## Limits

- **Single vantage point.** Every timestamp is when *this* node's stream delivered the message to
  the collector, on this host's clock. Latency is "first seen here" -> "validated here", not
  submission -> validation. A transaction submitted elsewhere is first seen here only after it
  has been relayed to this node, so real end-to-end latency is longer.
- **First-seen-proposed caveat.** `transactions_proposed` shows a transaction when this node applies
  it to its open ledger. Transactions this node first learns about through the consensus set, or
  while the collector was down, have no proposed sighting. They are counted as
  `validated_without_proposed` and kept out of the latency statistics. The `engine_result` of a
  proposed sighting is the node's **preliminary** result, which can differ from the final
  `TransactionResult`.
- **Ledgers, not seconds, for the outcome window.** Transactions with `LastLedgerSequence` beyond
  N ledgers could still validate later. Those show up as `late` when they do, or as `never` when the
  data ends, so N should be well above the network's typical LastLedgerSequence offset.
- **Testnet only.** The traffic is mostly test and automation accounts, so a single window says
  little about mainnet behaviour or fee escalation under real load.
- **Clock.** `t` comes from the host clock (NTP-synced). Sub-second figures share that error.
