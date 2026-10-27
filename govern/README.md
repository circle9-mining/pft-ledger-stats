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
| `check.sh` | ruff, shellcheck, unit tests and 11 mutants the tests must catch |

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

## 24h results (2026-10-26T16:10:36Z .. 2026-10-27T16:10:36Z)

Full report: [`report/REPORT.md`](report/REPORT.md) (header with OS, NTP state, window, flag-ledger and
validator/UNL counts; UNL tally per voting ledger; per-validator vote matrix), chart
[`report/yes-share.png`](report/yes-share.png), machine-readable [`report/summary.json`](report/summary.json)
and [`report/matrix.csv`](report/matrix.csv). Dataset: `data/govern-24h/govern-24h.jsonl.gz` (the raw
`govern.jsonl`, 35546 lines, sha256 of the uncompressed file `4a28b71a…` in full in `logs/post-window.log`)
plus `unl-start.json`, `unl-end.json` and `state.json`.

- Collection: one collector session, 0 resumes, 0 gaps, 28559 ledgers (7503448-7532006), 111/111 flag
  ledgers and 111/111 voting ledgers, 222 `ledger_entry` snapshots (Amendments + FeeSettings at each flag
  ledger), 0 snapshot errors, 0 bad lines, 0 duplicates dropped. No node restart (read-only).
- 61 validators (master keys) validated voting ledgers; 60 of them validated every one of the 111. 6763
  vote entries, all for the hash this node saw validated, 0 partial validations.
- **Nobody voted.** No validator, on or off the UNL, put an `amendments` or fee field in any voting-ledger
  validation, so UNL yes-share was 0% on all 111 voting ledgers, no Majorities appeared or disappeared,
  and the Amendments object held the same 64 enabled amendments from the first flag ledger to the last.
  FeeSettings stayed on the legacy fields (BaseFee `a`, ReferenceFeeUnits 10, ReserveBase 10000000,
  ReserveIncrement 2000000) all window. This is the real state of the testnet in this window, not a
  collector gap: the vote-field parsing, yes-share, vote-change and Majority logic is covered by the
  synthetic, labelled test cases above.
- UNL: 25 keys, quorum 17, identical at window start and end; all 25 validated all 111 voting ledgers
  and all run 1.0.9.
- Builds off the UNL: 19 x 1.0.4, 2 x 1.0.0, 1 x 1.0.8, 14 x 1.0.9. The 22 validators older than the
  newest UNL build (1.0.9) carry the `build-blocked-risk` flag.
- One non-UNL validator (`nHBcLk5r1D…`) has the `missing-vote` flag: no validation seen on 8 of the 111
  voting ledgers in its active span.
- Crosscheck: **20/20 MATCH** (`logs/crosscheck-20.log`, 2026-10-27T17:08Z). For 20 random flag ledgers
  (seed 20261026) the majorities recomputed from the collected raw UNL votes (25/25 validations, none
  voting) equal the public RPC's `Majorities` at the next flag ledger (none), with 64 enabled amendments
  on both sides. Two earlier attempts are kept: the public RPC was down 16:11-16:47Z (`crosscheck-20-stopped.log`),
  and right after it came back it answered the 5 oldest ledgers with `noNetwork` while it resynced
  (`crosscheck-20-partial.log`: 15/20, rc=1; 18 retries printed, a 6th ledger, 7511040, matched after its 3rd retry). Neither was a data mismatch.

### Logs

| log | what |
|---|---|
| `logs/collect-24h.log` | the 24h collector log (start, hourly progress, stop at window end) |
| `logs/unl-start.log`, `logs/post-window.log` | UNL snapshots at start and end; post-window run: OS, NTP, unit result, dataset sha256, analyzer output |
| `logs/check-1.log` .. `logs/check-4.log` | lint + pytest + mutants; check-2 and check-3 are failed runs kept as evidence (their fix is noted at the end), check-4 is the final run: 39 passed, 11/11 mutants caught |
| `logs/drill-1.log` | live SIGTERM + SIGKILL resume drill, torn tail repaired, 15/15 PASS |
| `logs/smoke-1.log`, `logs/partial-smoke-1.log`, `logs/partial-smoke-2.log` | live collector smoke, and analyzer + 3-ledger crosscheck smoke on a copy of the partial window (not the 24h run) |
| `logs/crosscheck-20-stopped.log` | first 20-ledger crosscheck after the window: stopped after 10 min because the public RPC did not answer at all (HTTP 000 from both hosts) |
| `logs/crosscheck-20-partial.log` | second attempt right after the RPC came back: 15/20, the 5 oldest ledgers still answered `noNetwork` after 3 retries each (RPC resyncing; 7511040 matched after 3 retries), rc=1 |
| `logs/crosscheck-20.log` | final crosscheck after the public RPC had resynced (complete_ledgers 7450054-...): **20/20 match**, 0 retries, rc=0 |
