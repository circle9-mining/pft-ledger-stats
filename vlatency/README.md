# vlatency: validation receive latency and signing-clock skew, per validator

`validators/` measures whether each validator participates. A validator can show 100% participation
and still be in trouble: overloaded, badly peered, or running a wrong clock. Its validations then
reach the rest of the network late, or carry a skewed `signing_time`. `vlatency/` watches the
`validations` and `ledger` streams of your own node and reports, per validator (signing key):

- **receive latency**: p50/p90/p99/max of how far each validation arrives behind the first
  validation of the same ledger (`d_first`), behind this node's `ledgerClosed` for that ledger
  (`d_closed`), and behind the ledger's network close time (`d_ct`, see the caveat below);
- **signing-clock skew**: `signing_time - receive time`, and how far the validator's median sits
  from the network median;
- **late-arrival share**: validations more than `--late` seconds (default 2.0) behind the first one;
- **non-validated-hash validations**: validations for a ledger index this node saw validated, but
  for a different hash; plus partial (`full: false`) validations;
- **active-span stats**: first..last ledger, and coverage over that span as well as over the whole
  window (a validator that joins mid-window is not reported as a fault);
- **hourly network distribution** of `d_first`.

It is read-only: the collector sends only `subscribe` (an allowlist in the code). Nothing is
submitted or signed, and the node is never restarted or reconfigured. The only external traffic is
the 20-ledger crosscheck against the public RPC.

## Files

| file | what |
|---|---|
| `collect_vlatency.py` | resumable stream collector -> `vl.jsonl` (append-only JSON lines) + `state.json` (window) |
| `run-collector.sh` | runs the collector inside the container's **network namespace** as an unprivileged user (same reason as `txflow/`) |
| `analyze_vlatency.py` | per-validator / network / hourly report -> `REPORT.md`, `summary.json`, `validators.csv`, 3 charts |
| `crosscheck_vlatency.py` | N random collected ledgers vs the local node AND a public RPC `ledger` call (hash, close_time) |
| `resume_drill.sh`, `vl_resume_check.py` | live SIGTERM + SIGKILL resume drill and its checks |
| `check.sh` | ruff, shellcheck, unit tests and 8 mutants the tests must catch |
| `report/` | the published report of the live window; `logs/` the redacted run logs |

Tests: `tests/test_vlatency_collect.py`, `tests/test_vlatency_analyze.py`, `tests/test_vlatency_resume.py`.

## Usage

```sh
# collection window (the live run used a transient systemd unit with Restart=on-failure and the same command)
sudo RUN_UID=$(id -u) RUN_GID=$(id -g) bash vlatency/run-collector.sh \
  --out data/vlatency --log data/vlatency/collect.log --hours 7
# crosscheck 20 random collected ledgers: run DURING or right after the window (see Limits)
python3 vlatency/crosscheck_vlatency.py data/vlatency/vl.jsonl --count 20
# report + charts (matplotlib, in the repo .venv)
.venv/bin/python vlatency/analyze_vlatency.py data/vlatency/vl.jsonl --report vlatency/report --charts
# lint + tests + mutants
bash vlatency/check.sh
# resume drill (separate output dir; 8-minute window; does not touch a running window)
bash vlatency/resume_drill.sh data/vlatency-drill-1
```

## Method

`vl.jsonl` events: `session` (each start: resumed?, last ledger, torn bytes cut, bad lines),
`ledger` (validated ledger: index, hash, `ct` = close_time, receive time `t`), `val` (the first
sighting of a (signing key, ledger index, ledger hash): `signing_time`, `validated_hash`,
`full`, master key, `t`), `gap` and `stop`. Gaps in the `ledger` stream are recorded with a reason
(`stream_skip`, `reconnect`, `restart`) and never interpolated. On restart the collector cuts a torn
last line, rebuilds the recent seen-set from the file, drops replayed validations, and resumes the
same window. The analyzer de-duplicates again over the whole file and does not depend on event
order (validations for a ledger arrive **before** its `ledgerClosed`, because the ledger is only
validated once a quorum of them is in).

**Times.** Ripple-epoch seconds (`signing_time`, `close_time`) are converted with
`unix = ripple + 946684800` (2000-01-01T00:00:00Z).

**Which latency to trust.**

- `d_first` (primary) is resolution-free: both ends are receive times on the same host clock.
  It mixes the validator's own speed with the relay path to this node.
- `d_closed` is relative to this node's `ledgerClosed` message. It is mostly negative, since the
  node announces the ledger once enough validations are in.
- `d_ct` is relative to the ledger's network close time, which is **coarse**. rippled rounds the
  agreed close time to `close_time_resolution` (10 s on this testnet; the possible values are
  10/20/30/60/90/120 s) and, if the rounded time would not move forward, uses
  `previous close_time + 1 s` instead (`effCloseTime`). With ~3 s ledgers and a 10 s resolution
  most ledgers are such "bumped" ledgers (ct = prev + 1). The analyzer classifies every ledger as
  `rounded` (ct is a multiple of the resolution), `bumped` (ct = previous ledger's ct + 1) or
  `other`, and computes `d_ct` over `rounded` ledgers only. Even there a single value is only good
  to about +-5 s, so `d_ct` can be negative. Use it to spot validators that are tens of seconds
  off, not for sub-second ranking. The crosscheck prints `close_time_resolution` and `close_flags`
  for every sampled ledger, so the resolution assumption is checked against the real ledgers.

**Skew.** `signing_time` is whole Ripple seconds (truncated) from the validator's network-adjusted
clock, and the receive time comes later than the signing. A healthy validator therefore sits at
roughly -0.3 .. -1.0 s. What matters is the distance from the network median (`skew-net`), not
the sign.

## Limits

- **Single vantage point.** Every `t` is when *this* node's stream delivered the message. A
  validator that is slow *to reach this node* (peering path) looks the same as one that is slow to
  sign. Compare against a second vantage point (e.g. a sentry) before blaming a validator.
- **First sighting only.** Relayed copies of the same validation are dropped. `d_first` is relative
  to the fastest validator *as seen here*, so it is a ranking, not an absolute network delay.
- **Close-time resolution** (above): `d_ct` is coarse by protocol design.
- **Host clock.** Receive times come from this host's NTP-synced clock. The run logs record the
  sync state. Sub-second skew figures share that error.
- **Crosscheck window.** The local node keeps only a few hundred ledgers (online_delete), so the
  crosscheck samples from the collected ledgers the local node still holds (the most recent
  ~25 minutes). Run it during or right after the window, and more than once if you want samples
  spread across the window.
- **Testnet only.** Validator count, load and peering differ from mainnet.
