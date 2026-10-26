# govern: who votes for what, per validator (amendments and fees)

`pft-amendments.py` reports the network-level picture: which amendments are enabled, which hold a
majority. `govern/` goes one level down and asks, for every validator this node hears from: on which
voting ledgers did it validate, which amendments did it vote for, did its votes change, what fee
values does it vote for, is it missing votes, and is it on an older build than the rest of the UNL.

It is read-only. The collector sends only `subscribe` and `ledger_entry` (an allowlist in the code),
the UNL snapshot runs the admin `validators` command, and nothing is submitted or signed. The node
is never restarted or reconfigured. The only external traffic is the 20-ledger crosscheck against the
public RPC (>= 1 s between requests, backoff on retries).

## How voting works (and what the collector records)

- A **voting ledger** is a ledger index with `(li + 1) % 256 == 0`. Validators put their amendment
  votes (`amendments`) and fee votes (`base_fee`, `reserve_base`, `reserve_inc` and the newer
  `*_drops` fields) into their validations of that ledger.
- The vote is tallied when the **flag ledger** `F = li + 1` closes. The resulting
  `EnableAmendment` / `SetFee` pseudo-transactions land in `F + 1`, so the effect shows in the
  `Amendments` / `FeeSettings` objects at the **next** flag ledger `F + 256` at the latest.
- An amendment gains a **majority** when at least 80% of the UNL vote for it. After a majority holds
  for the majority period (XRPL default 14 days; the period is not readable over RPC, so the
  projection says so) it is enabled.

The collector records every validation of a voting ledger, also the ones that carry no vote fields,
so "validated the voting ledger and voted for nothing" is distinguishable from "no validation seen".
At each flag ledger it reads both ledger objects with `ledger_entry`.

## Files

| file | what |
|---|---|
| `collect_govern.py` | resumable stream collector -> `govern.jsonl` (append-only JSON lines) + `state.json` (window) |
| `run-collector.sh` | runs the collector inside the container's **network namespace** as an unprivileged user (the admin WebSocket listens on the container's own loopback) |
| `unl_snapshot.sh` | read-only UNL snapshot (`validators` admin command inside the container): quorum, trusted master keys, master -> signing key map |
| `analyze_govern.py` | per-validator vote matrix, UNL tally per voting ledger, Majorities, fee settings -> `REPORT.md`, `summary.json`, `matrix.csv`, `yes-share.png` |
| `crosscheck_govern.py` | 20 random flag ledgers: majorities recomputed from raw votes vs public-RPC `Majorities` at the next flag ledger |
| `resume_drill.sh`, `gov_resume_check.py` | live SIGTERM + SIGKILL resume drill (with torn-tail repair) and its checks |
| `check.sh` | ruff, shellcheck, unit tests and 10 mutants the tests must catch |

Tests: `tests/test_govern_collect.py`, `tests/test_govern_analyze.py`, `tests/test_govern_crosscheck.py`,
`tests/test_govern_resume.py`. The real fixture `tests/fixtures/govern/probe-7501055.jsonl` is a
2026-10-26 capture around voting ledger 7501055 / flag ledger 7501056. No live validator sent vote
fields in it, so every amendment/fee vote, Majority and vote change in the tests is synthetic and
labelled as such.

## Usage

```sh
# UNL snapshot at window start (and unl-end.json at the end)
sudo bash govern/unl_snapshot.sh data/govern/unl-start.json
# 24h collection (the live run used a transient systemd unit with Restart=on-failure and this command)
sudo RUN_UID=$(id -u) RUN_GID=$(id -g) bash govern/run-collector.sh \
  --out data/govern --log data/govern/collect.log --hours 24
# report + chart (matplotlib, in the repo .venv)
.venv/bin/python govern/analyze_govern.py data/govern --report govern/report --charts
# crosscheck 20 random flag ledgers against the public RPC
python3 govern/crosscheck_govern.py data/govern --count 20
# lint + tests + mutants
bash govern/check.sh
# resume drill (separate output dir; 25-minute window so it spans a voting + flag ledger)
bash govern/resume_drill.sh data/govern-drill-1
```

## Method

`govern.jsonl` events: `session` (each start: resumed?, last ledger, torn bytes cut, bad lines),
`ledger` (validated ledger), `vote` (one per (signing key, voting ledger, ledger hash): `am`, `fee`,
`sv` = server_version, `extra` = unknown field names), `snap` (Amendments / FeeSettings at a flag
ledger, or the error), `gap` and `stop`. Gaps in the ledger stream are recorded with a reason
(`stream_skip`, `reconnect`, `restart`) and never interpolated. On restart the collector cuts a torn
last line and records how many bytes it cut.

The analyzer keys validators by master key (from the validation, else the UNL's signing-key map,
else the signing key prefixed `sk:`). It counts one vote per validator per voting ledger, only for
the hash this node saw validated. Per validator it reports voting ledgers validated, its active span,
missing votes (inside its active span; for UNL members, the whole window), yes-share per amendment
over the window and over the span, vote changes, fee-vote values and server version. **Build-blocked
risk** marks a validator whose build is older than the newest UNL build seen: an amendment only the
newer build supports would leave it amendment-blocked once enabled.

## Limits

- Single vantage point: a vote counts only if this node received the validation.
- The 80% UNL threshold is computed against the UNL size from `unl-start.json`. The UNL can change
  during a window; the report says whether `unl-end.json` differs.
- The local node keeps only ~512-1024 ledgers (`online_delete=512`), so the crosscheck uses the
  public RPC for ledger objects and the collected raw votes for the tally.
