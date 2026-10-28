# nunl: NegativeUNL history and disable/re-enable risk

The NegativeUNL (nUNL) lets the network keep reaching consensus when some trusted validators go
offline: validators that miss too many validations are voted onto the nUNL, which lowers the quorum,
and voted off again once they are reliable. `nunl/` answers two questions for Post Fiat testnet
operators:

1. **History:** which validators were ever put on or taken off the nUNL, when, and for how long?
   Taken from the ledger itself, over the full history the public RPC holds.
2. **Risk:** how close is each UNL validator to the disable threshold (or, if listed, to the re-enable
   threshold)? Measured per 256-ledger window from the validations your own node receives.

It is read-only. The collector sends only `server_info`, `ledger` and `ledger_entry` (an allowlist in the
code). The crosscheck sends only `ledger`, `ledger_entry` and the admin `ledger_request` to your own node.
Nothing is submitted or signed, and the node is never restarted or reconfigured.

## How the nUNL works in postfiatd (sources)

All references are to `postfiatorg/postfiatd` at `32723c15e686a2088f2c83017a6ff909d93822cd`:

| what | where |
|---|---|
| `negativeUNLLowWaterMark = FLAG_LEDGER_INTERVAL * 50 / 100` (128): a trusted validator with **fewer** validations than this in the last flag period is a disable candidate | `src/xrpld/app/misc/NegativeUNLVote.h` |
| `negativeUNLHighWaterMark = FLAG_LEDGER_INTERVAL * 80 / 100` (204): a listed validator needs **more** than this to be re-enabled | same |
| `negativeUNLMinLocalValsToVote = FLAG_LEDGER_INTERVAL * 90 / 100` (230): a node votes only if it validated more than this itself | same |
| `newValidatorDisableSkip = FLAG_LEDGER_INTERVAL * 2`, `negativeUNLMaxListed = 0.25` (at most 25% of the UNL listed) | same |
| comparisons `score < negativeUNLLowWaterMark` and `score > negativeUNLHighWaterMark` | `src/xrpld/app/misc/NegativeUNLVote.cpp` |
| nUNL voting runs in the consensus round whose previous ledger is a **voting ledger** (`(seq + 1) % 256 == 0`), i.e. for the flag ledger | `src/xrpld/app/consensus/RCLConsensus.cpp` |
| `applyUNLModify` rejects a UNLModify that is not in a flag ledger | `src/xrpld/app/tx/detail/Change.cpp` |
| when a flag ledger is built, `updateNegativeUNL()` moves the pending `ValidatorToDisable` / `ValidatorToReEnable` into `DisabledValidators` | `src/xrpld/app/ledger/detail/BuildLedger.cpp`, `src/xrpld/app/ledger/Ledger.cpp` |

So a disable vote that wins at flag ledger F shows up as a `UNLModify` tx in F and as
`ValidatorToDisable` in the NegativeUNL object at F. The validator is actually listed
(`DisabledValidators`) from flag ledger F + 256. Re-enabling works the same way.
The `NegativeUNL` amendment is enabled on the testnet (`feature NegativeUNL` → `enabled: true`).

## Files

| file | what it does |
|---|---|
| `collect_nunl.py` | walks every flag ledger F in the RPC's `complete_ledgers`. For each F it records the UNLModify txs in F, the NegativeUNL object at F and the object at F+1 (the following ledger). Output is resumable `nunl.jsonl`: each line is fsynced, a torn tail is cut on restart, and done flags are skipped. Retries use logged exponential backoff (`RETRY ... backoff=`) |
| `analyze_nunl.py` | builds per-validator event timelines, listed durations, the current state, 3 consistency checks, and 256-ledger risk tables. Writes `REPORT.md`, `summary.json` and `risk.csv`, and exits 1 if any check fails or a flag is missing |
| `crosscheck_nunl.py` | re-reads N random collected flag ledgers from your own node and compares hash, UNLModify txs, the NegativeUNL object and a positive control. `--corrupt K` is the negative control |
| `negative_control.sh` | corrupted-reconstruction negative control. It injects a fake `DisabledValidator` at one random flag ledger of a temp copy; the analyzer must exit 1 and name that ledger, and the unmodified copy must exit 0 |
| `check.sh` | runs ruff, shellcheck, 30 unit tests (synthetic fixtures, no network) and 15 mutants (6 threshold + 9 logic) that the tests must catch |
| `report/` | the published `REPORT.md`, `summary.json` and `risk.csv` |
| `logs/` | raw run logs (sanitized: home path → `~`) |

The risk table uses `vlatency/collect_vlatency.py` (the validation stream of your own node) as its
input, and `govern/unl_snapshot.sh` for the UNL (trusted master keys and the signing → master map).
Score statistics use `pftstats.stats.summarize`.

## Method

**History.** The analyzer reconstructs the nUNL at every flag ledger and checks the following:
1. the object at F+1 equals the object at F (the object only changes at flag ledgers);
2. the UNLModify txs in F are exactly the object's pending entries at F, and `LedgerSequence == F`;
3. `DisabledValidators(F) = DisabledValidators(F-256) + ToDisable(F-256) - ToReEnable(F-256)`.

It also checks coverage: every flag ledger F with F and F+1 inside the scanned range must be present.
If no events exist, the report says so explicitly, with the scanned flag-ledger count and ledger range.
Ledger-object keys are hex. The report converts them to the usual `n...` base58 node-key form so they
match UNL keys (the round trip is tested against a real UNL key).

**Risk.** For each voting ledger V seen by your node, the window is ledgers V-255 .. V. The window
is used only if the ledger stream delivered every ledger in it. A validator's score is the number of
ledgers in the window for which it signed a validation of the **validated** hash (wrong-hash
validations do not count). That is the quantity `NegativeUNLVote` compares against 128/204.
Status: `DISABLE_CANDIDATE` (< 128), `WATCH` (< 153, i.e. 50-60%, a report-only margin and not a
postfiatd constant), `OK`, and for listed validators `REENABLE` (> 204) / `STAYS_LISTED`.

**Crosscheck against your own node.** The testnet image runs with `online_delete=512`, so the local node
holds only the last ~512-1024 ledgers. To check random flag ledgers from the whole history,
`crosscheck_nunl.py` asks the node for each one with the admin command `ledger_request`. The node then
fetches the ledger from peers and accepts it only if it matches the hash in its own validated skip list.
Admin commands answer only on the container's loopback, so calls go through `docker exec`. Old flag
ledgers arrive in seconds. The ledger after a flag ledger (F+1) is usually not served by peers, so
F+1 is compared only when the node has it, and the log says so per ledger. As a positive control, the
`Amendments` object must be readable at F. That proves the state tree is present, so "NegativeUNL
absent" is a real answer.

## History limits

- The history is whatever the public RPC (`rpc.testnet.postfiat.org`) still holds in `complete_ledgers`
  (in the published run: about 2 days, from ledger 7500055). Older nUNL events cannot be seen from it.
  The report prints the exact range scanned.
- The risk table covers only the time your node's validation stream was recorded (a vantage-point
  measure: validations your node never received count as missing, just as they would for its own vote).
- `newValidatorDisableSkip` (new validators are not disabled for 2 flag periods) and the 25% cap are
  reported but not simulated per validator.

## Reproduce

```bash
# full-history collection over the public RPC (resumable; re-run the same command after an interruption)
python3 -u nunl/collect_nunl.py --out data/nunl/final --log data/nunl/final/collect.log

# validation stream for the risk table (inside the container's netns, see vlatency/README.md) + UNL snapshot
sudo RUN_UID=$(id -u) RUN_GID=$(id -g) bash vlatency/run-collector.sh --out data/nunl/vals --hours 3
sudo bash govern/unl_snapshot.sh data/nunl/unl-start.json

# analysis (exit 0 = all consistency checks pass, no flag missing)
python3 nunl/analyze_nunl.py --hist data/nunl/final/nunl.jsonl --vals data/nunl/vals/vl.jsonl \
  --unl data/nunl/unl-start.json --out nunl/report

# crosscheck 10 random flag ledgers against your node (needs sudo for docker exec), then the negative control
python3 -u nunl/crosscheck_nunl.py --hist data/nunl/final/nunl.jsonl -n 10
python3 -u nunl/crosscheck_nunl.py --hist data/nunl/final/nunl.jsonl -n 10 --corrupt 2   # must exit 1
bash nunl/negative_control.sh data/nunl/final/nunl.jsonl                             # analyzer must exit 1

# offline tests + mutants
bash nunl/check.sh
```

## Results (published run, 2026-10-28)

Full report: [`report/REPORT.md`](report/REPORT.md) (`summary.json`, `risk.csv` next to it).

| check | result | log |
|---|---|---|
| full-history collection, public RPC | 231/231 flag ledgers in 7500055-7559367 (first 219 in a fresh run, then 12 new ones on resume), 0 errors, 0 retries | `logs/07-collect-final.log`, `logs/12-collect-topup-final.log` |
| nUNL history | **no events**: 0 UNLModify txs and no NegativeUNL object at any of the 231 flag ledgers; 3 consistency checks pass | `logs/13-analyze-final.log` |
| risk, 256-ledger windows | 13 complete windows (voting ledgers 7556095-7559167, 3 h validation stream); all 25 UNL validators scored 256/256 in every window (status OK, margin 128 to the disable threshold) | `logs/13-analyze-final.log` |
| independent recount | a separate jq count over the raw stream gives 256 for all 25 UNL keys in the last window, the same as `risk.csv`; 63 keys were seen in total, and the only one below 256 (253) is not on the UNL | `logs/19-jq-recount-last-window.log` |
| crosscheck vs own node | 10/10 random flag ledgers OK (hash, UNLModify, NegativeUNL, Amendments positive control) | `logs/14-crosscheck-10-final.log` |
| crosscheck negative control | `--corrupt 2`: 8/10 OK, 2 MISMATCH, exit 1 | `logs/15-crosscheck-negative-control-final.log` |
| analyzer negative control | fake DisabledValidator at flag 7505408: exit 1, names F7505408 and F7505664; control copy exit 0 | `logs/16-analyzer-negative-control-final.log` |
| offline tests | ruff clean, shellcheck clean, 30/30 unit tests, 15/15 mutants caught | `logs/17-check-final.log` |
| code digests | sha256 of every script used in the runs above | `logs/18-digests-final.log` |

A testnet where every UNL validator signs every ledger is the expected healthy case, so the risk table
shows no candidates. The boundary cases (127/128 and 204/205 scores, listed validators, missing
ledgers in a window) are covered by the synthetic-fixture tests and the threshold mutants in `check.sh`.
