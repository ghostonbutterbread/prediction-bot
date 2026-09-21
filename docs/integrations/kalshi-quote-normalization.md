# Kalshi quote normalization — branch-local integration dossier

- Status: **committed, review-ready**; independent parent review pending.
- Owner: focused quote-normalization builder; parent owns Kanban `t_0bab6b93`, independent review and release.
- Branch / owning ref: `fix/kalshi-quote-normalization`.
- Worktree: `/home/ryushe/worktrees/prediction-bot-kalshi-quote-normalization`.
- Base: `4ca50c30c39707fd13e4b657d6299667cd761580` (`main`, lowest applicable owning ancestor).
- Intended integration target: **`beta` only**, observed at `02daa963`; NOT main.
- Updated: 2026-09-21 UTC.
- Immutable implementation/recovery checkpoint: `d3970f5db31c4c82c324a88edff7ad57d7efc79f`.
- Implementation commits: that checkpoint only; branch tip adds a dossier-only handoff commit.
- Review range: `4ca50c3..fix/kalshi-quote-normalization`; verify the later handoff commit separately.
- Handoff worktree state: clean after committing this dossier update.
- Inspiration: parent task's zero-ask beta replay and `beta_combined_lane_run_20260920T000332Z` evidence.

## Ownership and boundary

Repository AGENTS requires main-based implementation; `git merge-base main beta`
confirmed this base. `git fetch origin` succeeded before branching; local canonical
refs were explicitly assigned by the task. Divergent remote main was not merged,
reset or treated as the authorized base. Faulty quote parsing exists in main and
beta; beta's differences in this module preserve contract metadata, not quote
normalization. Implement once from the owning base; propagation to beta belongs
to the parent. No main/beta ref or active runtime checkout was edited.

Only `bot/exchanges/kalshi.py`, its tests and this temporary dossier/receipt are
changed. No history-reader, source-accuracy, pricing guard, risk, wallet, order,
configuration, timer or service changes. No merge, push, deployment or restart.
Public live API smoke was optional and not performed; HTTP is mocked in tests.

## Defects and implemented contract

1. `get_market_bid_ask` read only top-level cent fields. Nested `market` responses
   and dollar fields became four zeros, despite valid asks in listing data.
2. Raw market listing/detail parsing treated unsuffixed legacy cents as dollars.
3. Malformed quotes could drop an otherwise usable side or retain NaN, infinity,
   booleans/out-of-range values; a missing YES bid produced a fictitious half-ask midpoint.

One small raw quote helper now serves direct quotes and all raw listing quote
call sites. It reads dollar fields first, legacy cents otherwise, without a
magnitude heuristic. Non-null dollar values have precedence, including zero and
invalid values; null can fall back to an explicit legacy field. Each side is
independent. Non-finite, malformed, boolean, negative or >1 normalized values
become the existing 0.0 missing sentinel; valid [0,1] endpoints remain recorded.
This is quote capture, not new trade eligibility or fill/quantity assurance.
Midpoint/spread require both positive YES quotes. Crossed quotes remain observed
quotes (including negative spread); they are not silently repaired.

NO ask is **never** `1 - YES ask`, a bid, last trade, or generic market reference.
Missing sides remain zero, which also prevents downstream signal/complement
fallback in the existing execution-snapshot consumer for these responses.
Non-quote monetary fields and SDK dollar-only paths remain unchanged.

Trace exercised: `PredictionLab.run` → `_fetch_order_book` → actual mocked-HTTP
`KalshiExchange.get_market_bid_ask` → decision pipeline → stored collector
`decision_artifact.order_book`, `order_book_snapshot.data`, `execution_snapshot`.
Both normalized asks and BUY_NO selected price survive even on a SKIP, including
NO-only and reference-only responses. No capture caller change was necessary.
Source accuracy admission is untouched; no quote/trade eligibility guard added.

## Real test receipts

Machine-readable receipts and tested source hashes are tracked beside this file
in `kalshi-quote-normalization-receipts.json`. Full logs, scripts and raw-locator
audit evidence live in:

`/mnt/data-collection/prediction-bot/data/derived_reports/price_normalization_20260921T044200Z/`

- Initial RED shapes: 2 tests, 4 subtest failures, 0 errors (nested/dollar quotes
  read as zero; legacy listing values 43/59 rather than .43/.59).
- Second RED validity: 7 tests, 41 subtest failures, 0 errors.
- Final regression versus **base Kalshi module**: 8 tests, 83 subtest failures,
  4 errors (old malformed legacy input drops whole response); expected RED.
- Final focused: **105 tests, 0 failures/errors, 0 skips**.
- Final full discovery: **1052 tests, 0 failures/errors, 7 skip entries**. Skip
  entries are absent ignored runtime configs, including subtests; do not subtract
  them to manufacture a passed-test count.
- `git diff --check`: clean.

Exact final commands (from owned worktree):

```bash
P=/mnt/data-collection/prediction-bot/.venv/bin/python
R=/mnt/data-collection/prediction-bot/data/derived_reports/price_normalization_20260921T044200Z
$P "$R/run_tests.py" final-baseline-red --baseline test_kalshi_quotes
$P "$R/run_tests.py" final-focused test_kalshi_quotes test_kalshi_direct test_prediction_lab_collect test_collector_replay_inputs test_source_observation_ledger
$P "$R/run_tests.py" final-full
```

Use new receipt labels on rerun. Runner copies only task checkout source,
hash-verifies it, puts that copy first on PYTHONPATH, and verifies source hashes
before/after. CWD and module roots match. TMPDIR is outside source/data; only
scratch is writable via Landlock, networking denied via seccomp, environment
sanitized; the runtime interpreter and imports are recorded. Dependency pins
were compared with installed versions; no environment reinstall was needed.

Disclosed harness corrections: bubblewrap namespace setup was denied; the tested
Landlock/seccomp alternative supplied actual kernel enforcement. The first probe
caught CWD import precedence; an intermediate full run had 6 layout errors from
mixed original-module/copy-test roots and missing local data/summaries. Correcting
the disposable layout (not product code) yielded the full green result. An initial
capture fixture returned no strategy signal, so execution_snapshot was correctly
None; its corrected zero-confidence BUY_NO fixture exercises normalized storage
while still SKIP. Earlier failed receipts are retained, not presented as green.

## Historical recoverability (bounded, not all-history)

A read-only re-audit independently verified every frozen raw payload hash for
**6000 prior selected rows + 461 prior latest-snapshot rows = 6461 unique rows**.
It checks explicit asks at every nested path, including numeric strings and
legacy cents (broader than the previous numeric-only audit). **Zero rows have a
positive explicit ask; zero have retained nonempty quote arrays.** All have
generic yes/no price fields; these were NOT relabeled executable.

Current listing code maps ask fields into generic market prices, but that alone
does not prove each old row's original API field/unit or observation timestamp.
No historical quote projection is justified from the audited explicit fields;
none was added, and raw bytes were never rewritten. Older uninspected archive
coverage remains unknown. No current/future quote was fetched as historical.
The prior threshold30 summary still has maximum 17 and no as-of bucket set
meeting 30; quote repair does not address history maturity. Older-history work
and rerunning the beta comparison remain parent-owned, not claimed complete here.

## Review, activation and resume gates

- Independent review: **parent-owned, pending**; no nested reviewer launched.
- Integration: parent reviews exact branch range, preserves beta contract metadata,
  tests any combined beta candidate in isolation, then separately decides merge.
  No merge preflight was run in the live-imported beta checkout.
- Activation: separately authorized collector rollout and fresh quote-bearing
  snapshot proof required. Code merge alone does not activate the repair.
- Deferred live evidence: optional public read-only quote smoke plus a newly
  collected snapshot after an authorized rollout. Trigger: parent approval;
  required before claiming active collector repair, not before offline review.
- Historical economic replay: requires real recorded executable asks and eligible
  prior-only source history; this patch cannot synthesize missing observations.
- Resume point: parent reviews this dossier, receipt hashes and three code/test
  files; no more implementation expected unless review identifies a defect.
- Remove this dossier and companion receipt from the integration lane upon an
  accepted merge; retain them on the review branch/history for recovery.
