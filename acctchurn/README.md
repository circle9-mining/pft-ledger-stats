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
| `diff_churn.py` | checks both snapshots (header format, object count = `objects_paged`, unique `index`), diffs them per type, lists created/deleted AccountRoots, runs the conservation checks; exit 1 if any check fails, 2 if a snapshot is damaged |
| `funders_churn.py` | finds the creating transaction of every new account (the tx whose metadata has a `CreatedNode` AccountRoot for it) and builds the funding graph; exit 1 if any creating tx is not found |
| `crosscheck_churn.py` | checks seeded random accounts of snapshot B, every deleted account (balance at A, `actNotFound` at B) and each sampled account's creating tx against the public RPC; `--negative` adds 1 drop to every expected balance and must exit 1 |
| `report_churn.py` | writes `REPORT.md` (deterministic; addresses and hashes truncated to 8 characters) |

## Run it

```bash
# snapshot A now, snapshot B ~24 h later (own node, admin RPC via docker exec)
state/snapshot.py --out data/acctchurn/snapA
state/snapshot.py --out data/acctchurn/snapB

# fee burn over ledgers A+1..B, from an independent tx collector run over the same window
# (txmix/collect_tx.py --start A+1), summed from its tx.csv fee_drops column

acctchurn/diff_churn.py snapA.jsonl.gz snapB.jsonl.gz --fees-sum <drops> --out diff.json
acctchurn/funders_churn.py diff.json --reserve-base <drops from FeeSettings> --out funders.json
acctchurn/crosscheck_churn.py diff.json funders.json snapB.jsonl.gz --count 20 --seed 1
acctchurn/crosscheck_churn.py diff.json funders.json snapB.jsonl.gz --count 20 --seed 1 --negative  # must exit 1
acctchurn/report_churn.py diff.json funders.json --out acctchurn/REPORT.md

# tests + lint
.venv/bin/python -m pytest -q tests && .venv/bin/ruff check .
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
- **Never active** means the account's `Sequence` in snapshot B is still the `Sequence` it was created
  with (it has not sent a transaction).

## Tests

`tests/test_acctchurn_*.py`: counts and account lists on a fixture pair, exact conservation pass, a
fee-sum mismatch, a tampered balance (+1 drop), a dropped page (fewer objects than `objects_paged`), a
duplicate index, B not later than A, deterministic output, creation found after out-of-window entries,
creation before / after the window, retries counted, crosscheck positive/negative/wrong funder/deleted
account still present, and an end-to-end diff -> funders -> report run that checks no full address is
printed.

The published run (snapshots, logs, `REPORT.md`) is added after the 24 h window closes.
