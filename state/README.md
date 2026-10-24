# state/ — ledger-state snapshot analyzer

Takes one validated ledger's **entire state tree** from your own node and reports what is in
it: how many objects of each type exist, how PFT is distributed over accounts, how much of it
is locked as reserve, and how many trust lines, offers and other objects each holder owns.
Read-only: the only RPC methods it sends are `server_state`, `ledger`, `ledger_data` and
`account_info` (crosscheck only), through `docker exec <container> curl` to the admin port on
127.0.0.1. The admin port is never exposed.

| file | what it does |
|---|---|
| `snapshot.py` | pages `ledger_data` (binary=false, marker) for ONE pinned ledger, resumable, writes `snapshot-<ledger>.jsonl.gz` |
| `analyze.py` | offline: `REPORT.md`, `report.json`, `charts/*.png` from a snapshot |
| `crosscheck_state.py` | 10 random accounts vs `account_info ledger_index=<same>`, plus `--negative` control |
| `resume_drill.sh` | live SIGTERM + SIGKILL interrupt drill, then compares with an uninterrupted reference |
| `reproduce.sh` | rebuilds the committed report from the committed `.gz` and checks it byte for byte |
| `../tests/test_state.py` | unit tests: Gini/top-N/percentile on fixtures with known answers, marker resume, torn-line repair |

## Run it

```bash
# 1. snapshot the current validated ledger (~5k objects on testnet: 3 pages, a few seconds)
python3 state/snapshot.py --out data/state/run --log data/state/run/snapshot.log
#    options: --ledger N   --limit 2048 (objects/page)   --container postfiatd   --no-sudo
# 2. crosscheck right away (see "Retention" below), then the negative control (must exit 1)
python3 state/crosscheck_state.py data/state/run/snapshot-N.jsonl.gz --count 10 --seed 1
python3 state/crosscheck_state.py data/state/run/snapshot-N.jsonl.gz --count 10 --seed 1 --negative
# 3. report
.venv/bin/python state/analyze.py data/state/run/snapshot-N.jsonl.gz --out state/report
# tests / lint / reproduce
.venv/bin/python -m pytest -q tests/test_state.py && .venv/bin/ruff check state tests && shellcheck state/*.sh
bash state/reproduce.sh
```

Exit codes. `snapshot.py`: 0 done, 4 stopped by SIGTERM/SIGINT before the last page (re-run to
resume), 3 the pinned ledger is gone from the node. `crosscheck_state.py`: 0 all match, 1 any
mismatch, 2 too few accounts checked. `analyze.py`: 1 if the type counts don't add up.

## Method

- **One ledger, pinned.** The first start reads `server_state` and pins the validated ledger's
  index **and hash** in `state.json`, along with the `ledger` header (`total_coins`,
  `account_hash`, close time). Every page asks for that `ledger_index`, and a page whose
  `ledger_hash` differs is rejected, so a snapshot never mixes ledgers.
- **Resume.** Each page goes to `pages.jsonl` as one line (page number, marker in/out,
  objects). It is flushed and fsynced before the next request. On restart the file is checked
  (numbering, marker chain, ledger) and a torn last line from a SIGKILL is cut. Paging then
  continues from the last complete page's marker. The `.gz` is written only after the last
  page, with gzip mtime=0, sorted JSON keys and no page count or wall-clock time in it. So the
  same ledger gives **the same bytes** for any page size and any number of interruptions,
  which the live drill checks.
- **Balances.** `AccountRoot.Balance` in drops; the median/p90/p99 are linear-interpolated
  percentiles. Top-N share = the N largest balances / the total. Gini =
  `2·Σ i·x_i / (n·Σx) − (n+1)/n` over ascending balances (0 = all equal, (n−1)/n = one account
  holds everything).
- **Reserves.** `locked = min(Balance, ReserveBase + ReserveIncrement × OwnerCount)`. The values
  come from the snapshot's own `FeeSettings` object, so they belong to the same ledger. The
  report also compares them with `server_state` `reserve_base`/`reserve_inc` recorded at pin
  time. Spendable = Balance − locked.
- **Per holder.** A `RippleState` counts for both its `LowLimit` and `HighLimit` accounts.
  Offers = `Offer` + `NFTokenOffer`. "Other" = `NFTokenPage` (owner = the first 20 bytes of its
  index), owner `DirectoryNode`s and any other object with an `Account`/`Owner` field.
- **total_coins.** The ledger header's `total_coins` must equal the sum of AccountRoot balances
  plus native PFT held in `Escrow` and `PayChannel` objects (Amount − Balance). The report
  prints the difference. Burned fees leave `total_coins`, so they are not part of it.

## Retention

The testnet-light image ships `[node_db] online_delete=512`, so the node keeps only the last
~512–1024 ledgers (about 30–60 minutes). `account_info ledger_index=<snapshot>` and a resume
only work while the pinned ledger is still held. Crosscheck right after the snapshot. If an
account is answered with `lgrNotFound`/`actNotFound`, the crosscheck skips it and takes
another random account (after 3 `lgrNotFound` with nothing checked it exits 2). A resume after
the ledger is gone exits 3 instead of mixing ledgers.

## Published run — ledger 7429722 (2026-10-24T02:14:20Z)

Report: [`report/REPORT.md`](report/REPORT.md) · data: [`../data/state/snapshot-7429722.jsonl.gz`](../data/state/snapshot-7429722.jsonl.gz) · logs: [`../logs/state/`](../logs/state/)

- 4850 objects: 4331 AccountRoot, 298 NFTokenPage, 115 LedgerHashes, 52 DirectoryNode,
  45 NFTokenOffer, 4 RippleState, 3 Offer, 1 Amendments, 1 FeeSettings. Type counts add up to
  the 4850 objects paged.
- PFT is extremely concentrated: one account holds ~97.0 of the ~100 billion PFT, Gini 0.9997,
  top-10 hold 99.48%. The median account holds 14.99999 PFT (3693 of 4331 accounts hold exactly
  14999990 drops, i.e. 15 PFT minus one 10-drop fee).
- Reserve-locked: 44,009.999768 PFT (10 PFT base + 2 PFT/owned object). 6 accounts hold less
  than their own reserve.
- `total_coins` 99,999,999,969,468,888 drops = sum of AccountRoot balances, a difference of 0.
  This ledger has no Escrow or PayChannel objects. Assuming an XRPL-style genesis supply of exactly
  100,000,000,000 PFT, 30.531112 PFT has been burned as fees so far, and burned fees are outside `total_coins`.

| proof | log |
|---|---|
| interrupt drill: SIGTERM (exit 4) → resume, SIGKILL (137) → resume, 76 pages of 64; snapshot sha256 `9ad84962…` **byte-identical** to an uninterrupted 3-page reference | [`1-resume-drill.log`](../logs/state/1-resume-drill.log) |
| 10 random accounts vs `account_info ledger_index=7429722`: 10/10 MATCH, exit 0 | [`2-crosscheck.log`](../logs/state/2-crosscheck.log) |
| negative control (+1 drop): 10/10 MISMATCH, exit 1 | [`3-crosscheck-negative.log`](../logs/state/3-crosscheck-negative.log) |
| report generation | [`4-analyze.log`](../logs/state/4-analyze.log) |
| `reproduce.sh`: REPORT.md + report.json sha256 identical | [`5-reproduce.log`](../logs/state/5-reproduce.log) |
| unit tests 11/11 (suite 53/53), ruff + shellcheck clean | [`6-tests.log`](../logs/state/6-tests.log) |

The SIGKILL in the live drill landed between two page writes, so the last byte of
`pages.jsonl` was a newline and no torn line had to be cut. The torn-line repair is covered by
`test_marker_resume_after_interrupt_and_torn_line` (half a JSON line appended, then resume).

## Privacy

The `.gz` is the public ledger state as any node serves it through `ledger_data`, so it
contains on-chain addresses. The report and logs contain only aggregates and ledger object
IDs. The crosscheck prints AccountRoot indexes, never addresses. Logs start with the OS line
and contain no IPs, host names or local paths.
