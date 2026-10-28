# NegativeUNL history and risk report

Scanned flag ledgers: **231/231** in ledger range **7500055-7559367** (first flag 7500288 at 2026-10-26T13:31:21Z, last flag 7559168 at 2026-10-28T14:59:31Z); missing 0, unresolved errors 0.

UNLModify pseudo-transactions found: **0**; flag ledgers with a NegativeUNL object: **0**.

**No NegativeUNL events exist in the scanned history**: no validator was voted onto or off the NegativeUNL in ledgers 7500055-7559367 (231 flag ledgers), and the NegativeUNL object did not exist at any of them.

Current state (last flag ledger): disabled 0 [], pending disable None, pending re-enable None.

## Consistency checks

1. NegativeUNL object at F+1 equals F (changes only at flag ledgers)
2. UNLModify txs in F match the object's pending ValidatorToDisable/ValidatorToReEnable at F
3. DisabledValidators(F) = DisabledValidators(F-256) + ToDisable(F-256) - ToReEnable(F-256)

Result: **PASS** (0 failures)


## Event timeline

No events.


## Validation-share risk (256-ledger windows ending at voting ledgers)

Thresholds from postfiatd NegativeUNLVote.h: disable candidate if score < 128 (50%), re-enable if score > 204 (80%); a node votes only with > 230 own validations; at most ceil(25% x UNL) listed. WATCH (< 153, 60%) is a report-only margin.

UNL snapshot 2026-10-28T12:11:03Z: 25 trusted validators, quorum 17, max listed 7.

Complete windows seen by this node: **13** (voting ledgers 7556095-7559167, validation stream ledgers 7555792-7559362).

| validator | windows | last score | last share | min score | min window | worst status | margin to disable |
|---|---|---|---|---|---|---|---|
| `nHB6Zc7mhr7swksEgpwTE7Hw7SvZ9cz22T2MECMSXjBeMTeHXQB7` | 13 | 256 | 100.0% | 256 | 7556095 | OK | 128 |
| `nHBAToXoTH6eZtC1cMJDvjG7eRb7ufZ3GUpeisvNVT4ofruRdVvd` | 13 | 256 | 100.0% | 256 | 7556095 | OK | 128 |
| `nHBM2nzq3pZUg8JsxvEt3G7gAAtc5Sukaef6YmvVx64uAoRK4QWM` | 13 | 256 | 100.0% | 256 | 7556095 | OK | 128 |
| `nHBVTAUpHEoEctXcAWyC7nJhxUYULWKz1WZCuC1tSm2VnCxDshVS` | 13 | 256 | 100.0% | 256 | 7556095 | OK | 128 |
| `nHBWFVzxVYAVQNUFHoqnHw2SWhXKy1kKRr7oYAMFeu22rqsCLYCN` | 13 | 256 | 100.0% | 256 | 7556095 | OK | 128 |
| `nHBYKjxjbzxRzrS3XkhpgM7KXJ25jbgPHkbAjjSCQPm8PaPS5y4v` | 13 | 256 | 100.0% | 256 | 7556095 | OK | 128 |
| `nHBb2uHHY8V78qWQT3P6ADkFz4pRyEgJpA2pEqmQyTHuxXHccBzM` | 13 | 256 | 100.0% | 256 | 7556095 | OK | 128 |
| `nHBcLEB4S6moQGrhMjJo1jbp58WL5psHY9EMDWNAtdqykUYiA1rF` | 13 | 256 | 100.0% | 256 | 7556095 | OK | 128 |
| `nHBgZupJspDsrg7mex4Z2vBQXqoN2aTHaLgf8jGxMDPw4MWLh3J6` | 13 | 256 | 100.0% | 256 | 7556095 | OK | 128 |
| `nHBmj7zUY2yMc9f5wgNcCuWj79BUieNmc1GG8PqiEP1tgNZZWLVM` | 13 | 256 | 100.0% | 256 | 7556095 | OK | 128 |
| `nHBxQ3srN8ErMvfMgsJ3jycSSaMFyHMk5WbrJVUTXPZgNh1VdbHH` | 13 | 256 | 100.0% | 256 | 7556095 | OK | 128 |
| `nHBxxNuWsJXkzLk332ADxt1T18N1e9BSYjekjGNVCPfV813tSdvx` | 13 | 256 | 100.0% | 256 | 7556095 | OK | 128 |
| `nHByMXejvHJgjcGJ1f9bhcAGcFeNR6ecsDmzN4t3HkhyRHZtM6Lj` | 13 | 256 | 100.0% | 256 | 7556095 | OK | 128 |
| `nHD3sPmhNtVXTXrZAGhsfHRhxhzFXKHNNxemTtGFr69SRd9q88dZ` | 13 | 256 | 100.0% | 256 | 7556095 | OK | 128 |
| `nHDfSHLutR7QqttAkx3AiYG2bukCdfAa3P9hrYL4yNJZQpvEpW1A` | 13 | 256 | 100.0% | 256 | 7556095 | OK | 128 |
| `nHDw5erSdzhtZ7khjHeotRywM6WyPgJp6sUSewscoqLgC6LGATzn` | 13 | 256 | 100.0% | 256 | 7556095 | OK | 128 |
| `nHU74qX4tCQDSpE6zBS5PB3jybuGZJ7QMbeyLWDQRy3Lhb4DYDSR` | 13 | 256 | 100.0% | 256 | 7556095 | OK | 128 |
| `nHUCEXpC5LhFAm1Mmf8TqrzVGt3QCuwWoW2V8PYynDpjZe8m8mHj` | 13 | 256 | 100.0% | 256 | 7556095 | OK | 128 |
| `nHUUXMXfPEdnKAT8u2AB89LxTWT1tWsTecDPQURoMw2XJ2WP85MK` | 13 | 256 | 100.0% | 256 | 7556095 | OK | 128 |
| `nHUWciHX8W9PgM3sQmgkRiKpkuaJjTxFSyvZce4bP4WeMz81HefX` | 13 | 256 | 100.0% | 256 | 7556095 | OK | 128 |
| `nHUatddiVB2GN6zHHCk1gtepjANF8BdjPKuVcN6jwG2JwBYPea3k` | 13 | 256 | 100.0% | 256 | 7556095 | OK | 128 |
| `nHUdwzTWTQJzebbxcanZG2ERXikMLU9aAZa8cHtxosfiKq5N7Vd5` | 13 | 256 | 100.0% | 256 | 7556095 | OK | 128 |
| `nHUvKQbxx14GFyMaRB4vgu11x6pXrwMwX9yP5Ltgk4jp4SHbbgWA` | 13 | 256 | 100.0% | 256 | 7556095 | OK | 128 |
| `nHUwdHGzvTM93oV38tMPSWZAWQ2Q6hVDS3XLQ32HPTUtcRASpayQ` | 13 | 256 | 100.0% | 256 | 7556095 | OK | 128 |
| `nHUzc5XzsmweRV6aNyUQJD5eRUM4TT4tVCBv5DKx6T7Buq4gn9Lr` | 13 | 256 | 100.0% | 256 | 7556095 | OK | 128 |

Worst status counts: {'OK': 25}

Scores over all 325 (window, validator) pairs (pftstats.summarize): min 256, median 256, p95 256, max 256, mean 256.00.


Overall: **PASS**

