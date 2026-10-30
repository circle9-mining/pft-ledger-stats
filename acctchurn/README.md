# acctchurn: account churn and funding between two ledger-state snapshots

`acctchurn/` compares two full ledger-state snapshots of the Post Fiat testnet taken about 24 hours
apart and answers:

1. **Churn:** which ledger objects (by `LedgerEntryType`) were created, deleted or changed, and which
   accounts appeared or disappeared.
2. **Conservation:** does every drop add up? Native PFT held in the state (account balances, escrows,
   unclaimed payment-channel funds) must equal the header's `total_coins` in each snapshot, and the
   difference between the two must equal the PFT burned as fees in between, to the drop.
3. **Funding:** who funded each new account, with how much (in base reserves), whether the new account
   ever sent a transaction, and whether new accounts funded further new accounts (chains).

It is read-only. Snapshots come from [`state/snapshot.py`](../state/README.md) on your own node.
`funders_churn.py` and `crosscheck_churn.py` send only `account_tx`, `tx` and `account_info` to the
public RPC (an allowlist in the code, shared with `rewardflow/`). Nothing is submitted or signed.

## Files

| file | what it does |
|---|---|
| `feesum_churn.py` | sums `fee_drops` over ledgers A+1..B from the tx collector's `ledgers.csv` + `tx.csv`; refuses (exit 2) if any ledger is missing, if a ledger's tx rows differ from its `txn_count`, or if a duplicated (resume) row conflicts; writes account-free `fees-ledgers.csv` / `fees-tx.csv` |
| `diff_churn.py` | checks both snapshots (header format, object count = `objects_paged`, unique `index`), diffs them per type, lists created/deleted/changed AccountRoots (changed: balance, Sequence, OwnerCount at A and B), net and gross balance changes, runs the conservation checks; exit 1 if any check fails, 2 if a snapshot is damaged |
| `funders_churn.py` | finds the creating transaction of every new account (the tx whose metadata has a `CreatedNode` AccountRoot for it) and builds the funding graph; exit 1 if any creating tx is not found |
| `crosscheck_churn.py` | checks `--count` seeded random accounts drawn from the CREATED + CHANGED accounts (at B: snapshot B's Balance/OwnerCount; at A: snapshot A's, or `actNotFound` for a created one), every deleted account (balance at A, `actNotFound` at B) and each sampled new account's creating tx against the public RPC; `--negative` adds 1 drop to every expected balance and must exit 1 |
| `report_churn.py` | writes `REPORT.md` and, with `--chart`, `balance-changes.png` (both deterministic; addresses and hashes truncated to 8 characters) |
| `mutants_churn.py` | mutation check: puts one broken line back at a time in a temp copy and requires the acctchurn tests to fail (10 mutants) |

## Run it

```bash
# snapshot A now, snapshot B ~24 h later (own node, admin RPC via docker exec)
state/snapshot.py --out data/acctchurn/snapA
state/snapshot.py --out data/acctchurn/snapB

# fee burn over ledgers A+1..B, from an independent tx collector run over the same window
# (txmix/collect_tx.py --start A+1 --hours 24.5)
acctchurn/feesum_churn.py txwin/ledgers.csv txwin/tx.csv --from <A+1> --to <B> --out-dir acctchurn/data

acctchurn/diff_churn.py snapA.jsonl.gz snapB.jsonl.gz --fees-sum <drops> --out diff.json
acctchurn/funders_churn.py diff.json --reserve-base <drops from FeeSettings> --out funders.json
acctchurn/crosscheck_churn.py diff.json funders.json snapB.jsonl.gz --count 20 --seed 1 | tee xc.log
acctchurn/crosscheck_churn.py diff.json funders.json snapB.jsonl.gz --count 20 --seed 1 --negative | tee xcn.log  # must exit 1
acctchurn/report_churn.py diff.json funders.json --out acctchurn/REPORT.md --chart acctchurn/balance-changes.png \
    --crosscheck xc.log --crosscheck xcn.log --tests "<suite result>"

# tests + lint + mutants
.venv/bin/python -m pytest -q tests && .venv/bin/ruff check . && .venv/bin/python acctchurn/mutants_churn.py
```

## Method notes

- **Native PFT in the state** = sum of `AccountRoot.Balance` + `Escrow.Amount` + (`PayChannel.Amount` -
  `PayChannel.Balance`). Token (IOU) amounts are objects (dicts) and are not counted. On the first real
  snapshot (ledger 7570042) this sum equals `total_coins` exactly (0 drops difference).
- **The ledger window is enforced by the client.** The public RPC (postfiatd 1.0.9) echoed
  `ledger_index_min`/`ledger_index_max` back but returned transactions from before `ledger_index_min`
  (asked for 7569990-7570000, got ledgers 7500711, 7500978, 7500983). `funders_churn.py` therefore skips
  entries before the window and stops at the first entry after it (results are in forward order). A test
  server that ignores the window, like the real one, covers this.
- **RPC errors are not results.** The public RPC sometimes answers `lgrNotFound` or `internal` for a
  ledger inside its own `complete_ledgers`, and the same query works seconds later. `funders_churn.py`
  and `crosscheck_churn.py` retry those (and `slowDown`/`tooBusy`/... and transport errors) up to 6
  times with doubling backoff, count each retry by error (`retries_by_error`), and never count one as a
  mismatch; if they persist the tool exits 2 (RPC failure), not 1. `actNotFound` is an answer, not retried.
- **Fees are the only burn here.** Every transaction in a validated ledger pays its fee, whatever its
  result code (`tec...` included), so the fee sum over A+1..B is the expected `total_coins` drop.
- **Big integers:** drop totals are above 2^53. `jq` stores numbers as doubles and rounds them (log 08
  shows 99999999968988550 as ...540); the JSON files hold exact integers, read them with Python (log 12).
- **Never active** means the account's `Sequence` in snapshot B is still the `Sequence` it was created
  with (it has not sent a transaction).

## Tests

`tests/test_acctchurn_*.py`: counts and account lists on a fixture pair, exact conservation pass, a
fee-sum mismatch, a tampered balance (+1 drop), a dropped page (fewer objects than `objects_paged`), a
duplicate index, B not later than A, deterministic output, creation found after out-of-window entries,
creation before / after the window, retries counted, crosscheck positive/negative/wrong funder/deleted
account still present, and an end-to-end diff -> funders -> report run that checks no full address is
printed. Added for the real run: fee-sum completeness (missing ledger, dropped tx row, identical and
conflicting duplicates), changed rows and net/gross aggregates, an OwnerCount-only change, the
created+changed sample pool (an unchanged account is never sampled), `lgrNotFound`/`internal` retried
and counted (and a persistent one = RPC failure, not a mismatch), `actNotFound` not retried, report
balance/RPC sections and a byte-identical chart. Suite: 228 passed; `mutants_churn.py` 10/10 caught
(log 07; M7 was first written as an equivalent mutant and survived, so it was replaced).

## Results: ledgers 7570042 -> 7598607 (2026-10-29T00:08:11Z -> 2026-10-30T00:08:22Z)

See [`REPORT.md`](REPORT.md). Headlines:

- **Conservation exact to the drop.** total_coins fell by 98,456 drops; the fee sum of all 9,827
  transactions in the 28,565 ledgers A+1..B is 98,456 drops; native PFT held in the state equals
  `total_coins` in both snapshots (escrow and payment channels hold 0). All 5 checks PASS.
- **Churn:** 15 accounts created, 0 deleted, 49 changed; NFTokenPage 24 created / 21 deleted / 19 changed;
  no trust line, offer, escrow or directory changes.
- **Funding:** all 15 new accounts were created by Payments from one funder (`r9k51cJF…`), 100 PFT each
  (10 base reserves); all 15 sent at least one transaction; no funding chains.
- **Crosschecks:** 20 of 64 created+changed accounts, at A and B, plus 2 creating transactions:
  42/42 OK, 0 retries; the negative control gave 40 mismatches (the 2 `actNotFound` checks carry no
  balance) and exit 1.

## Published data and logs

`data/`: both snapshots (`snapshot-7570042.jsonl.gz`, `snapshot-7598607.jsonl.gz`), `fees-ledgers.csv`,
`fees-tx.csv`, `diff.json`, `funders.json`. `logs/` (raw run logs; home directory paths are replaced with
`~`, nothing else changed):

| log | what |
|---|---|
| 01 | snapshot A |
| 02 | public RPC `account_tx` window probe (server ignores `ledger_index_min`) |
| 03 | snapshot B, first attempt: failed because the `--log` directory did not exist (kept) |
| 04 | snapshot B |
| 05, 06 | fee sum + diff and funders on the earlier code (1a6ed24); superseded by 08/09 on the final code, same numbers |
| 07 | full test suite, ruff, mutants |
| 08 | fee sum + diff (final code) |
| 09 | funders (final code) |
| 10 | crosscheck |
| 11 | crosscheck negative control (exit 1 expected) |
| 12 | exact totals, report + chart, chart byte-identical on re-run, full-address grep (0 = none; grep exits 1 on 0 matches) |
