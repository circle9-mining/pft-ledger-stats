# rewardflow: reward-payer PFT outflows over the public RPC's whole history

`txmix/`, `txflow/` and `validators/` look at 24h windows of all traffic on our own node. `rewardflow/`
looks at one account over everything the public RPC still holds: the Task Node reward-payer
(`rwdm72S9YVKkZjeADKU2bbUMuY4vPnSfH7`, found as the sender of the reward payments to our own
wallet) and the PFT it pays out. It answers: how much is paid per hour and day, to how many recipients,
how concentrated, in what sizes, how bursty, and under which memo category (task reward vs airdrop).

It is read-only and needs no node: only `server_info`, `account_tx`, `tx` and `account_info` are sent
(an allowlist in `collect_rewardflow.py`), at most 2 requests/s (collector) and 1 request/s
(crosscheck), to `https://rpc.testnet.postfiat.org`. Nothing is signed or submitted.

## Files

| file | what |
|---|---|
| `collect_rewardflow.py` | resumable forward `account_tx` collector with marker pagination -> `data.jsonl` |
| `analyze_rewardflow.py` | payouts per hour/day, sizes, concentration, gaps, memos -> `REPORT.md` + 3 charts |
| `crosscheck_rewardflow.py` | 20 random payments refetched via `tx` (14 fields each) + balance reconciliation |
| `check.sh` | offline checks: ruff, shellcheck, unit tests, 4 mutants the tests must catch |
| `run-live.sh` | live run: paged + interrupted + resumed collect, independent one-pass collect, compare, analyze, crosscheck |
| `report/` | the published report and charts |
| `logs/` | raw tee'd logs of every live run (see "Published runs") |

Tests: `tests/test_rewardflow.py` (fixtures and a fake RPC, no network).

## Usage

```sh
A=rwdm72S9YVKkZjeADKU2bbUMuY4vPnSfH7
python3 rewardflow/collect_rewardflow.py $A --out data/rewardflow/data.jsonl     # stdlib only
.venv/bin/python rewardflow/analyze_rewardflow.py data/rewardflow/data.jsonl --out-dir rewardflow/report  # matplotlib
python3 rewardflow/crosscheck_rewardflow.py data/rewardflow/data.jsonl --count 20  # exit 0 = PASS
bash rewardflow/check.sh                                                         # offline tests
bash rewardflow/run-live.sh $A logs-dir                                          # the full published run
```

Run from the repo root. Any account works; the report wording assumes a payer account.

## Method

**Collector.** On the first start it reads `server_info.complete_ledgers` and pins the range
(`ledger_min`-`ledger_max`) into the first line of `data.jsonl`. It then pages `account_tx`
forward (`forward: true`, `--limit` per page, `marker` until none). Each validated transaction becomes one
fsynced JSON line with ledger_index, close time, hash, type, sender, destination, `Amount`,
`delivered_amount`, result, flags, fee, decoded memo type/format/kind, direction (out/in/other) and
`meta_balance_delta` (the account's own AccountRoot Balance change from the metadata). A final `done` line
records the range and counts.

**Resume.** Restarting with the same `--out` keeps the pinned range, cuts a torn last line, and restarts
paging at the last stored ledger **inclusive**, skipping stored hashes. Inclusive matters: two payments
can share a ledger, and a page boundary can fall between them (the unit test builds exactly that case;
mutant M1, "resume after the last ledger", loses a payment and is caught). `slowDown`/`tooBusy`/
network errors are retried with exponential backoff; any other RPC error stops the run (exit 2).

**What counts as a payout.** Outgoing `Payment`, result `tesSUCCESS`, native amount. The paid value is
always `delivered_amount`, never `Amount` (mutant M2 is caught). Non-`tesSUCCESS` outgoing payments are
reported as failed and not summed (mutant M3 is caught). Partial payments (tfPartialPayment, or
delivered < Amount) are counted. Incoming payments (funding) are listed under "other" and not summed.

**Metrics.** PFT per UTC day and hour; count; unique recipients; size p50/p90/p99/max and size buckets;
top-10 share, HHI (sum of squared recipient shares) and Gini over recipient totals; repeat-recipient
rate (recipients paid more than once); seconds between consecutive payments (p50/p90/p99/max); memos by
decoded MemoType/MemoFormat and `kind`, the `<word>` of the first `<word>_<hex>` token in MemoData
(`task`, `airdrop`). MemoData itself is not published. Addresses in the report are cut to 6 characters.

**Crosschecks.** (1) 20 seeded-random successful payments are fetched again with `tx`, the record is
rebuilt from that answer with the same code, and all 14 fields must be equal. (2) For the full range and
3 sub-ranges `(L1, L2]`, `account_info` Balance at L2 minus at L1 must equal both the sum of collected
`meta_balance_delta` and the sum derived from transaction fields alone (-Fee, -delivered out,
+delivered in), to the drop. A range starting one ledger before the server's history is always added: it
must come back IMPOSSIBLE (reported with the RPC error and never counted as a match). If no range can be
checked at all, the reconciliation fails.

## Limits

- **"Whole history" = the public server's history.** `complete_ledgers` started at 7400054 in every run
  (first payment 2026-10-23T01:23:32Z), about 2.4 days. The payer's Sequence is ~6500, so most of its
  history is older than the server holds and is not in this report. Balance reconciliation is impossible
  for any range starting before 7400054.
- **Transient answers.** In run 1 the public server answered `account_info` at ledger 7400054, inside its
  own complete_ledgers, once with `lgrNotFound` and once with `internal`, and one `tx` refetch (ED1388…,
  ledger 7400152) differed in delivered_amount/result/meta_balance_delta. A minute later (run 2) 7400054
  answered but 7400055 gave `lgrNotFound`; in run 3 both ledgers answered 12/12 and the tx 8/8, all
  from one node (`logs/run2-probe.log`, `logs/run3-repeat-queries.log`). The crosscheck now retries such answers up to 4
  times with backoff and prints every retry (mutant M4, "no retry", is caught). A server that keeps
  giving them still produces IMPOSSIBLE / MISMATCH, never a silent pass.
- **Small n.** 82 payments to 12 recipients. Percentiles and concentration over that are descriptive
  only. The 3 payments of 0.000001 PFT are shown as they are (they look like test/dust payments).
- Memo `kind` is a heuristic over the MemoData bytes, not a decoder of the `pf.ptr` v4 format.
- Our own wallet is one of the 12 recipients (`rHv6jS`).

## Published runs (`logs/`)

| run | files | result |
|---|---|---|
| 1 (10:13Z) | `run1-collect.log`, `run1-analyze.log`, `run1-crosscheck.log` | collect PASS; crosscheck **FAIL**: 19/20 refetch, 2 ranges IMPOSSIBLE although inside history (transient answers, see Limits) |
| 2 (10:14Z) | `run2-probe.log` | the failing queries of run 1 asked again by hand |
| 3 (10:15Z) | `run3-repeat-queries.log` | 12x repeat: all answers identical, one node |
| 4 (10:17Z) | `run4-*.log` | after the retry fix: crosscheck PASS; report showed memo `kind` = other for all (parser bug: the protobuf length byte `,` is printable and stuck to the token) |
| 5 (10:20Z) | `run5-tests.log`, `run5-collect.log`, `run5-analyze.log`, `run5-crosscheck.log` | **final**: 19 tests + 4/4 mutants, compare PASS, 20/20 refetch, 4/4 ranges reconciled, out-of-history range IMPOSSIBLE as expected; `report/` is from this run |

The run-4 memo fix has its own regression test (`test_memo_kind_real_shape`, which uses the live byte shape).
Run 4's crosscheck and collect results are otherwise the same as run 5.

## Privacy

`data.jsonl` holds full addresses and is not committed (`.gitignore`). The report shows 6-character
prefixes only. Logs contain the payer address (a public Post Fiat service account), tx hashes and
ledger numbers, which are all public ledger data.
