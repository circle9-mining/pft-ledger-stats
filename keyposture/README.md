# keyposture: signing-key posture of every account, reconciled exactly between two snapshots

`keyposture/` classifies how every Post Fiat testnet account can sign transactions (master key,
regular key, multi-signing) at two complete ledger-state snapshots about 24 hours apart. It flags
weak configurations, and proves that snapshot A plus every key-relevant transaction in between
gives exactly snapshot B, per account and per class.

It is read-only. Snapshots come from [`state/snapshot.py`](../state/README.md) and transactions
from `collect_key.py`, both read on your own node (admin JSON-RPC via `docker exec`, read-only
methods only). `crosscheck_key.py` sends only `account_info` and `tx` to the public RPC (an
allowlist in the code). Nothing is submitted or signed.

## Classes

An account's posture is (master key enabled?, `RegularKey`, `SignerList` quorum + entries):

| class | master | regular key | signer list |
|---|---|---|---|
| `master_only` | yes | - | - |
| `master+regular` | yes | yes | - |
| `master+multisig` | yes | - | yes |
| `master+regular+multisig` | yes | yes | yes |
| `regular_only` | disabled | usable | - |
| `multisig_only` | disabled | - (or black hole) | yes |
| `regular+multisig` | disabled | usable | yes |
| `blackholed` | disabled | black-hole address | - |
| `no_usable_key` | disabled | - | - |

Black-hole addresses are the well-known keyless ones (`rrrrrrrrrrrrrrrrrrrrrhoLvTp`,
`rrrrrrrrrrrrrrrrrrrrBZbvji`, `rrrrrrrrrrrrrrrrrNAMEtxvNvQ`, `rrrrrrrrrrrrrrrrrrrn5RM1rHd`).

Weak-configuration flags (`posture_key.weaknesses`): `master_still_enabled` (a regular key or
signer list exists but the master key can still sign alone), `single_signer_meets_quorum`,
`quorum_unreachable`, `one_signer_entry`, `self_in_signer_list`, `regular_key_in_signer_list`,
`regular_key_is_self`, `blackholed`, `no_usable_key`.

## Files

| file | what it does |
|---|---|
| `posture_key.py` | posture model: classes, weak flags, base58 address decoding, AccountRoot and SignerList keylets |
| `count_key.py` | completeness check of a snapshot: pages the same ledger again with `ledger_data` type filters `account` and `signer_list`, records the marker chain, and requires the index sets to equal the snapshot's (exit 0/1/2); `--offline COUNT_JSON` makes no RPC call and re-checks a published snapshot against a recorded result (below) |
| `collect_key.py` | resumable collector: follows validated ledgers from `--start`, writes every key-relevant tx (below) with full tx JSON and metadata to `key-tx.jsonl`, one `ledgers.csv` row per ledger (with `key_tx_count`), `gaps.csv` for ledgers it could not fetch; a torn or orphan line is repaired on resume |
| `resume_drill_key.sh` | resume drill on a fixed ledger range: uninterrupted reference run vs a run that gets SIGTERM, then SIGKILL, resumed each time; `key-tx.jsonl` must be non-empty and byte-identical, every ledger present once, no gaps |
| `reconcile_key.py` | completeness checks C1-C5 (exit 2), replay A -> B and residual checks R1-R5 (exit 1, every residual listed); `--negative` must exit 1 |
| `stats_key.py` | class counts and shares (accounts and PFT), weak flags, top balances per class, transitions, posture of the Task Node reward payer and its recipients (from `rewardflow/`), balance-bucket chart; refuses a failed or `--negative` reconciliation (exit 2) |
| `crosscheck_key.py` | public-RPC checks K1-K3 (below); `--negative` alters every expected value and must exit 1 |
| `drill_next_key.sh` | waits on your own node for the next key-relevant tx, then runs `resume_drill_key.sh` on a range around it while the node still holds those ledgers (exit 3 if none appears in time) |
| `report_key.sh` | writes `REPORT.md` from `reconcile.json`, `stats.json`/`stats.md`, the snapshot state files and the crosscheck SUMMARY lines (no hand-typed numbers) |
| `mutants_key.py` | mutation check: puts one broken line in at a time (in a temp copy of the repo) and requires the keyposture tests to fail (25 mutants) |

A tx is **key-relevant** if its type is `SetRegularKey`, `SignerListSet` or `AccountSet` (any
result), or its metadata touches a `SignerList`, creates or deletes an `AccountRoot`, or modifies an
`AccountRoot` whose `RegularKey` changed or whose `lsfDisableMaster` bit flipped.

## Run it

```bash
# snapshot A + completeness check (run count_key.py right away: the node keeps ~1000 ledgers),
# then start the collector at A+1 for 24.5 h (own node)
state/snapshot.py --out data/keyposture/snapA --log data/keyposture/snapA/snapshot.log
keyposture/count_key.py data/keyposture/snapA/snapshot-A.jsonl.gz --json data/keyposture/snapA/count.json
keyposture/collect_key.py --out data/keyposture/tx --log data/keyposture/tx/collect.log --hours 24.5 --start <A+1>
# resume drill on an already-validated range that contains key-relevant txs, or wait for the next one
keyposture/resume_drill_key.sh <first> <last> data/keyposture/drill-1
keyposture/drill_next_key.sh data/keyposture/drill-1 6
# ~24 h later: snapshot B + its count check (the collector must have reached B)
state/snapshot.py --out data/keyposture/snapB --log data/keyposture/snapB/snapshot.log
keyposture/count_key.py data/keyposture/snapB/snapshot-B.jsonl.gz --json data/keyposture/snapB/count.json

keyposture/reconcile_key.py --snap-a A.jsonl.gz --snap-b B.jsonl.gz --tx data/keyposture/tx \
  --count-a snapA/count.json --count-b snapB/count.json --json reconcile.json
keyposture/reconcile_key.py --snap-a A.jsonl.gz --snap-b B.jsonl.gz --tx data/keyposture/tx --negative  # exit 1
keyposture/stats_key.py --reconcile reconcile.json --snap-a A.jsonl.gz --snap-b B.jsonl.gz \
  --rewardflow data/rewardflow/data.jsonl --json stats.json --md stats.md --chart keyposture/balances.png
keyposture/crosscheck_key.py reconcile.json A.jsonl.gz B.jsonl.gz --count 20 --seed 1 --extra <payer>
keyposture/crosscheck_key.py reconcile.json A.jsonl.gz B.jsonl.gz --count 20 --seed 1 --extra <payer> --negative

.venv/bin/python -m pytest -q && .venv/bin/ruff check . && .venv/bin/python keyposture/mutants_key.py
```

## Method notes

- **SignerList owner by keylet.** SignerList objects carry no `Account` field. The owner is found
  by computing `SHA-512Half(0x0053 || AccountID || uint32 0)` for every account, the definition in
  postfiatd `src/libxrpl/protocol/Indexes.cpp` (`LedgerNameSpace::SIGNER_LIST = 'S'`,
  `signers(account, 0)`). The same code computes AccountRoot keylets (`0x0061 || AccountID`), and
  `reconcile_key.py` checks that against the index of every AccountRoot in both snapshots, which
  tests the address decoding on real data.
- **Completeness.** C1 every ledger A+1..B once in `ledgers.csv` and none in `gaps.csv`; C2
  `key_tx_count` = `key-tx.jsonl` lines per ledger; C3 no duplicate tx hash; C4 both snapshots
  parse and hold `objects_paged` objects, B after A; C5 every AccountRoot index = its keylet,
  every SignerList has a live owner, and `count_key.py` passed for the same ledger.
- **Replay** order is `(ledger_index, TransactionIndex)`, whatever the line order in the file.
  Within one tx, AccountRoot creations/modifications are applied first, then SignerList changes,
  then AccountRoot deletions (metadata lists nodes by index, and an AccountDelete deletes both).
- **Residuals** (exit 1): R1 per-account posture and account set equal snapshot B; R2 per class
  count(A) + in - out = count(B); R3 replayed SignerList index set = B's; R4 metadata consistency
  (created account already live, modified/deleted account not live, a `PreviousFields`
  RegularKey/flag that differs from the replay, or an unchanged field that differs from it);
  R5 every account whose posture differs A->B is touched by a collected tx.
- **--negative** drops the first collected tx that has an effect. If no tx in the window has one,
  it flips the master flag of snapshot A's first account (sorted by address) instead; the log
  line `NEGATIVE ...` says which.
- **Crosscheck** against the public RPC (lgrNotFound / internal / slowDown retried with backoff and
  counted per error in the SUMMARY line, never counted as mismatches; exit 2 if they persist):
  K1/K2 `account_info` with `signer_lists: true` at B and at A gives the same posture as the
  snapshot (actNotFound = absent) for every account that is not `master_only` at A or B, every
  account whose posture changed, every `--extra` account, and seeded others up to `--count`;
  K3 up to `--count` txs with an effect are validated, in the same ledger, same type, tesSUCCESS.
- Addresses are truncated in `REPORT.md`. The raw snapshots and `key-tx.jsonl` keep full
  addresses: this is public ledger data and the reconciliation needs them to be reproducible.

## Tests

`tests/test_keyposture_*.py` use small hand-built snapshots (valid addresses and keylets), tx
files and a fake public RPC. They cover every class and weak flag, a clean A->B reconciliation
with regular key, signer list, master disable, account creation and deletion, the negative
control (both modes), tampered B postures, class counts, SignerList sets, replay order, R4 cases,
missing/gap/count/duplicate/malformed inputs, bad snapshots and count files, the keylet definition,
crosscheck per check (API v1 and v2 `signer_lists`), retry accounting, the read-only allowlist,
stats refusal and determinism, and collector resume after a stop, an orphan line and torn tails.

## Results

Window: snapshot A ledger 7614111 (closed 2026-10-30T13:10:43Z) -> snapshot B ledger 7643241
(closed 2026-10-31T13:41:30Z), 29130 ledgers. Full tables in [`REPORT.md`](REPORT.md); chart
[`balances.png`](balances.png).

- **Every account is `master_only`, at A and at B.** 4433 accounts at A, 4448 at B (15 created by
  Payments, 0 deleted), 0 `SignerList` objects, 0 regular keys, 0 disabled master keys, so no weak
  flag fires (a flag needs a regular key, a signer list or a disabled master). That includes the
  Task Node reward payer and all 12 recipients found by `rewardflow/`, and the 10 largest balances.
- **Exact reconciliation, 0 residuals:** `master_only` 4433 + 15 - 0 = 4448 = B; `RESULT PASS
  residuals 0` (`logs/06-reconcile-and-negative.log`). The `--negative` control (first account
  creation dropped) fails with 3 residuals (R1, R2, R5), exit 1, same log.
- **Completeness:** collector `SUMMARY window 2026-10-30T13:11:31Z -> 2026-10-31T13:41:31Z ledgers
  7614112-7643241 fetched=29130 key_tx=15 gaps=0`, no `gaps.csv` (`logs/05-...`); `count_key.py`
  re-paged both ledgers with type filters: `RESULT PASS` for A (`logs/02`) and B (`logs/03`).
  Snapshot B is pinned to the collector's last ledger (7643241), so A+1..B is exactly the window.
- **Resume drill:** no key-relevant tx inside the window was still on the node when the window
  closed (the last one, ledger 7642730, had been pruned by `online_delete`), so `drill_next_key.sh`
  waited for the next one after B (Payment creating an account, ledger 7644462) and ran the drill on
  7644362..7644472: SIGTERM, then SIGKILL, resumed each time; `key-tx.jsonl` byte-identical to the
  reference (1 line, sha256 `9bf8da4fe4de3b668211b43bae4a81e8cd270d0725f85085d81a183eb1283439`), every ledger once, no gaps, `RESULT: PASS`
  (`logs/04-resume-drill.log`). The drill range lies after B and is not part of the reconciliation.
- **Crosscheck (public RPC):** `SUMMARY checked=55 mismatches=0 accounts=20 required=16
  effect_txs=15 negative=False rpc_calls=55 retries=0 retries_by_error={}` (`logs/08`): 20
  accounts at A and B (the 15 created + the payer are required; there is no non-`master_only`
  account to add) and all 15 txs with an effect. `--negative`: `checked=55 mismatches=55`, exit 1
  (`logs/09`). Retry accounting is exercised by a unit test (fake RPC answers lgrNotFound twice; the
  SUMMARY must show `retries=2` and the error name, `tests/test_keyposture_crosscheck.py`); this
  run needed none.
- **Tests / lint / mutants:** `358 passed` (full suite; 55 keyposture tests), ruff and ruff format
  clean, shellcheck clean, `MUTANTS 25 caught=25 failed=0` (`logs/10-tests-ruff-mutants.log`).

### Offline re-check of the snapshot counts

The node prunes old ledgers, so `count_key.py`'s paging cannot be repeated later. `--offline`
re-reads the committed snapshot and checks it against the committed `count_key.py` result, with no
RPC call: same ledger index and hash, continuous marker chains whose page sizes sum to the recorded
paged count, no recorded differences, and the snapshot file's AccountRoot/SignerList count equal to
the recorded paged count. The recorded file holds counts and marker chains, not the paged index
lists, so this reproduces the counts; index-set equality was checked when the ledger was paged
(logs 02/03). Exit 0 / 1 (count differs) / 2 (bad file, other ledger, broken chain).

```bash
keyposture/count_key.py keyposture/data/snapshot-7614111.jsonl.gz --offline keyposture/data/snapA-count.json
keyposture/count_key.py keyposture/data/snapshot-7643241.jsonl.gz --offline keyposture/data/snapB-count.json
```

### Data (`data/`)

| file | what |
|---|---|
| `snapshot-7614111.jsonl.gz`, `snapshot-7643241.jsonl.gz` | full state snapshots A and B (`state/snapshot.py`) |
| `snapA-*.json`, `snapB-*.json`, `snap*-snapshot.log` | snapshot headers (ledger hash, close time), `count_key.py` results, paging logs |
| `tx/key-tx.jsonl`, `tx/ledgers.csv`, `tx/collect.log`, `tx/state.json` | collector output for ledgers 7614112..7643241 |
| `drill-1/` | resume drill reference and drill outputs + their collector logs, and the watcher's output |
| `reconcile.json`, `stats.json`, `stats.md` | `reconcile_key.py --json`, `stats_key.py --json/--md` |

### Logs (`logs/`, redacted: home directory -> `~`, username -> `<user>`)

| log | what |
|---|---|
| 01 | snapshot A, **failed**: the `--log` directory did not exist (kept as run) |
| 02 | snapshot A + `count_key.py` (PASS) |
| 03 | snapshot B at the collector's last ledger + `count_key.py` (PASS) |
| 04 | resume drill, started by `drill_next_key.sh` on the first key-relevant tx after the window |
| 05 | collector end (0 gaps) and copy of the data into `keyposture/data` with sha256 of source and copy |
| 06 | reconcile (PASS) and `--negative` (FAIL, exit 1) |
| 07 | stats + chart, re-run byte-identical |
| 08 | crosscheck, public RPC (0 mismatches, 0 retries) |
| 09 | crosscheck `--negative` (every check MISMATCH, exit 1) |
| 10 | full test suite, keyposture tests, ruff, shellcheck, 25 mutants |
| 11 | `REPORT.md` generation, re-run byte-identical |
| 12 | offline count re-check from clean checkouts: `count_key.py` at eb5bda9 has no offline mode (exit 2 without a node); with `--offline`, snapshots A and B PASS, wrong count file exit 2; tests, ruff, 29 mutants |

The collector itself was started as a transient systemd unit on 2026-10-30 without a separate
tee'd launch log; its own `tx/collect.log` records the start (`WINDOW START ... at ledger 7614112`),
hourly progress and the final SUMMARY.
