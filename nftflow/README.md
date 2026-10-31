# nftflow: NFT lifecycle and exact supply reconciliation between two ledger-state snapshots

`nftflow/` explains who minted, burned and transferred which NFTs on the Post Fiat testnet over a
~24-hour window, and proves that the numbers add up exactly:

1. **Supply:** snapshot B's NFTs must equal snapshot A's plus every mint minus every burn in
   ledgers A+1..B, per issuer and overall, with each NFT on the owner the replay says.
2. **Counters:** each issuer's `MintedNFTokens` / `BurnedNFTokens` (AccountRoot) must move by
   exactly the mints and burns the replay found.
3. **Lifecycle:** hourly activity, issuers, flags and taxons, transfer fees, mint-to-burn lifetime,
   offer fates and NFTokenPage fill, computed only from a reconciliation that passed.

It is read-only. Snapshots come from [`state/snapshot.py`](../state/README.md) and transactions
from `collect_nft.py`, both on your own node (admin JSON-RPC via `docker exec`, read-only
methods only). `crosscheck_nft.py` sends only `tx`, `account_nfts`, `account_info` and
`ledger_entry` to the public RPC (an allowlist in the code). Nothing is submitted or signed.

## Files

| file | what it does |
|---|---|
| `collect_nft.py` | resumable collector: follows validated ledgers from `--start`, writes every NFToken* tx (and any tx whose metadata touches an NFTokenPage or NFTokenOffer) with full tx JSON and metadata to `nft-tx.jsonl`, one `ledgers.csv` row per ledger (with `nft_tx_count`), `gaps.csv` for ledgers it could not fetch; a torn or orphan line is repaired on resume, so SIGTERM or SIGKILL cannot duplicate or lose a ledger |
| `resume_drill_nft.sh` | resume drill on a fixed ledger range: an uninterrupted reference run vs a run that gets SIGTERM and then SIGKILL and is resumed each time; `nft-tx.jsonl` must be byte-identical, every ledger present once, no gaps |
| `reconcile_nft.py` | completeness checks (every ledger A+1..B exactly once, `gaps.csv` empty, `nft_tx_count` = jsonl lines per ledger, no duplicate tx hash, both snapshots intact; exit 2 otherwise), then replays each tx's NFTokenPage diffs onto snapshot A and runs R1-R5 (below); exit 1 with every residual listed; `--negative` drops the first mint and must exit 1 |
| `stats_nft.py` | lifecycle statistics + hourly mint/burn chart from a passing reconciliation (refuses a failed or `--negative` one, exit 2); mint field check: every minted ID's flags, fee, issuer and taxon must equal the mint tx's fields (exit 1) |
| `crosscheck_nft.py` | public-RPC sampling checks X1-X5 (below); `--negative` alters every expected value and must exit 1 |
| `report_nft.py` | writes `REPORT.md` from the reconcile and stats JSON (every number comes from them; accounts truncated to 8 characters); refuses a failed, `--negative` or other-window input (exit 2) |
| `mutants_nft.py` | mutation check: puts one broken line in at a time (in a temp copy of the repo) and requires the nftflow tests to fail (19 mutants) |

## Run it

```bash
# snapshot A, then start the collector at A+1 for 24.5 h (own node)
state/snapshot.py --out data/nftflow/snapA
nftflow/collect_nft.py --out data/nftflow/tx --log data/nftflow/tx/collect.log --hours 24.5 --start <A+1>
# resume drill on an already-validated range
nftflow/resume_drill_nft.sh <first> <last> data/nftflow/drill-1
# ~24 h later: snapshot B (the collector must have reached B)
state/snapshot.py --out data/nftflow/snapB

nftflow/reconcile_nft.py --snap-a A.jsonl.gz --snap-b B.jsonl.gz --tx data/nftflow/tx --json reconcile.json
nftflow/reconcile_nft.py --snap-a A.jsonl.gz --snap-b B.jsonl.gz --tx data/nftflow/tx --negative  # must exit 1
nftflow/stats_nft.py --reconcile reconcile.json --snap-a A.jsonl.gz --snap-b B.jsonl.gz \
  --tx data/nftflow/tx --json stats.json --chart nftflow/hourly.png
nftflow/crosscheck_nft.py reconcile.json A.jsonl.gz B.jsonl.gz --count 20 --owners 3 --seed 1
nftflow/crosscheck_nft.py reconcile.json A.jsonl.gz B.jsonl.gz --count 20 --owners 3 --seed 1 --negative  # exit 1

# tests + lint + mutants (matplotlib is only needed by the chart tests)
.venv/bin/python -m pytest -q && .venv/bin/ruff check . && .venv/bin/python nftflow/mutants_nft.py
```

## Method notes

- **Effects come from page diffs, not from tx types.** For each tx, the NFTokens arrays of every
  NFTokenPage it created, modified or deleted are compared before and after (the page owner is
  the first 20 bytes of the page index). An ID that appears is a *mint*, one that disappears is a
  *burn*, one whose owner changes is a *transfer*, and one that only moves to another page of the
  same owner (a page split or merge) is a *move*, which changes nothing. A token that stays on a
  page the tx modified (it is listed before and after) is no effect at all. This catches NFT changes
  made by any tx type, and the crosscheck then compares the derived ID with the server's own
  `nftoken_id` for sampled txs (X1).
- **Replay order** is `(ledger_index, TransactionIndex)`, whatever the order of lines in the file.
- **Reconciliation checks** (exit 1, every residual listed):
  R1 the replayed NFTokenID -> owner map equals snapshot B's (a mint of a live ID, or a burn or
  transfer of an ID that is not live with that owner, is also a residual);
  R2 per issuer and in total, live(B) = live(A) + mints - burns;
  R3 per issuer, `MintedNFTokens` and `BurnedNFTokens` deltas (absent = 0) equal mints and burns;
  R4 the replayed offer set equals snapshot B's offers (created / deleted NFTokenOffer nodes);
  R5 every minted NFTokenID is new (not live at A, not minted twice).
- **Crosscheck** against the public RPC, with lgrNotFound / internal answers retried and counted
  per error in the SUMMARY line (never counted as mismatches; exit 2 if they persist):
  X1 seeded mints, burns and transfers: `tx` is validated, same ledger, same type, tesSUCCESS,
  same NFTokenID; X2 the largest owner at B plus seeded others: the full paged `account_nfts` at B
  equals snapshot B's set; X3 sampled burned IDs are absent at B; X4 every issuer with a mint or
  burn: counters at A and at B equal the snapshots'; X5 seeded live offers via `ledger_entry` at B,
  deleted offers answer `entryNotFound`.
- **Lifetime is left-censored:** burns of NFTs minted before A have no mint time in the window and
  are counted separately, not as short lifetimes.
- Addresses and hashes are truncated in `REPORT.md`. The raw `nft-tx.jsonl` and snapshots keep full
  addresses: this is public ledger data and the reconciliation needs them to be reproducible.

## Tests

`tests/test_nftflow_reconcile.py`, `tests/test_nftflow_stats.py` and `tests/test_nftflow_crosscheck.py`
use small hand-built snapshots, tx files and a fake RPC server. They cover a clean reconciliation,
the negative control, a tampered owner / counter / offer set in B, a damaged snapshot, missing,
duplicated and gap ledgers, count mismatches, duplicate tx hashes, page splits, deleted pages,
a mint of a live ID, a burn by the wrong owner, replay order, re-mint of an ID live at A, collector
torn/orphan-line repair, stats refusal of failed or negative runs, the mint field check, crosscheck
mismatch detection per check, retry accounting, the read-only method allowlist, untouched tokens on a
modified page (not moves), and the report's truncation, determinism and refusals (`tests/test_nftflow_report.py`).

## Results

Window: snapshot A ledger 7599848 (2026-10-30T01:11:12Z) -> snapshot B ledger 7628391 (2026-10-31T01:11:20Z),
2417 NFT transactions. Full tables in [`REPORT.md`](REPORT.md); chart [`hourly.png`](hourly.png).

- **Exact reconciliation, 0 residuals:** live NFTs 4898 + 1339 mints - 1078 burns =
  5159, R1-R5 PASS (`logs/07-reconcile-and-negative.log`). The `--negative` control (first mint
  dropped) fails with 4 residuals, exit 1.
- **Collection:** `SUMMARY window 2026-10-30T01:11:25Z -> 2026-10-31T01:41:25Z ledgers 7599849-7628988 fetched=29140 nft_tx=2474 gaps=0` (`logs/06-...`); no `gaps.csv`. Resume drill (SIGTERM + SIGKILL,
  byte-identical output): `RESULT: PASS` (`logs/04-resume-drill.log`).
- **Activity:** 49 of 160 issuers minted or burned; the top 3 made 20.5% of all
  mints + burns. 0 transfers and 0 offers created in the window (45 offers at A and B,
  all untouched): NFT activity here is mint/burn churn, not trading. Every mint carried a URI and
  every TransferFee is 0.
  Busiest hour: 13 (43 mints, 140 burns).
- **Lifetime:** 674 NFTs were minted and burned inside the window (median 8459 s, p90 41931 s);
  404 burns were of NFTs minted before A (left-censored).
- **Pages:** mean fill 16.38 -> 16.43 tokens of 32
  (314 pages at B); 798 page moves (split/merge), no ownership change.
- **Crosscheck (public RPC):** `SUMMARY checked=181 mismatches=0 ledgers=7599848->7628391 mints=20/1339 burns=20/1078 transfers=0/0 owners=3/147 issuers=49 offers_live=20/45 offers_deleted=0/0 negative=False rpc_calls=177 retries=0 retries_by_error={}` (`logs/12-crosscheck.log`); `--negative`: `SUMMARY checked=181 mismatches=181 ledgers=7599848->7628391 mints=20/1339 burns=20/1078 transfers=0/0 owners=3/147 issuers=49 offers_live=20/45 offers_deleted=0/0 negative=True rpc_calls=177 retries=0 retries_by_error={}`, exit 1 (`logs/13-...`).
- **Tests / mutants:** `327 passed in 11.11s`, ruff clean, shellcheck clean; `MUTANTS 19 caught=19 failed=0` (`logs/14-tests-ruff-mutants.log`).

### Data (`data/`)

| file | what |
|---|---|
| `snapshot-7599848.jsonl.gz`, `snapshot-7628391.jsonl.gz` | full state snapshots A and B (`state/snapshot.py`) |
| `snapA-state.json`, `snapB-state.json`, `snap*-snapshot.log` | snapshot headers (ledger hash, close time, total_coins) and paging logs |
| `tx/nft-tx.jsonl`, `tx/ledgers.csv`, `tx/collect.log`, `tx/state.json` | collector output for ledgers 7599849..7628988 (it ran 30 min past B; reconcile uses A+1..B) |
| `drill-1/` | resume drill reference and drill outputs + their collector logs |
| `reconcile.json`, `stats.json` | `reconcile_nft.py --json` and `stats_nft.py --json` for the window |

### Logs (`logs/`, redacted: home directory -> `~`, username -> `<user>`; the redactor also wrote the CRLF
CSV rows quoted in log 03 with LF line endings)

| log | what |
|---|---|
| 01 | snapshot A, **failed**: the `--log` directory did not exist (kept as run) |
| 02 | snapshot A |
| 03 | collector launch (transient systemd unit, 24.5 h from A+1) |
| 04 | resume drill: SIGTERM, then SIGKILL, resumed each time; byte-identical to the reference |
| 05 | snapshot B |
| 06 | collector end (exit 0, 0 gaps) and copy of the data into `nftflow/data` with sha256 of source and copy |
| 07 | reconcile (PASS) and `--negative` (FAIL, exit 1) |
| 08 | stats + chart, re-run byte-identical |
| 09, 10, 11 | **superseded**: first crosscheck, crosscheck `--negative` and test runs. Run 10 left 14 X4 checks OK (issuers with no AccountRoot at A kept the expectation "absent" under `--negative`); fixed in `crosscheck_nft.py`, test + mutant M19 added, re-run as 12-14 |
| 12 | crosscheck, public RPC (0 mismatches, 0 retries) |
| 13 | crosscheck `--negative` (every check MISMATCH, exit 1) |
| 14 | full test suite, nftflow tests, ruff, shellcheck, 19 mutants |
| 15 | `REPORT.md` generation |
