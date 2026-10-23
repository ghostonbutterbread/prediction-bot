# Kalshi per-series pagination integration dossier

- **Status:** corrected code independently APPROVED and full regression/catalog gates passed; integration/activation BLOCKED on operator decision at the shared runtime boundary below.
- **Owner:** parent reviewer, Kanban `t_d694dfa0`; implementation subagent owns only this worktree.
- **Canonical path:** `docs/integrations/kalshi-series-pagination.md` (temporary branch-local handoff).
- **Branch / owning ref:** `fix/kalshi-series-pagination-t_d694dfa0`.
- **Worktree:** `/home/ryushe/worktrees/prediction-bot-kalshi-pagination-t_d694dfa0`.
- **Base commit:** `46806cf736917d3c6106c4766aa5e94f35f66a8d`.
- **Intended integration target:** `beta`, observed at the base above, also matching `origin/beta` when inspected.
- **Initial implementation commit:** `2eec119aa5cf1e34f8185a4ccba72d1e3a7e9995`. The initial reviewed tip was `b1d64a7e4a2ed3bd898a3d66dd99410ddb4e95ed`; the cursor-correction checkpoint is pinned in the follow-up handoff below.
- **Last updated:** 2026-10-22 PDT (2026-10-23 UTC).
- **Inspiration:** user requirement to follow collector pagination; retained public-catalog validation showing 84 quote-eligible contracts beyond the first ten per series.
- **Supersedes / successor:** implementation and code-review gates are complete. Resume only after operator decision on quiescing/activation; no stable promotion is authorized.

## Intent and boundaries

Repair only direct collector market discovery. Previously each weather series requested at most ten listings and discarded the continuation cursor. Rejected/closed listings could also consume the global quota before the final freshness filter.

Explicit assignment targets the owning `beta` lineage, overriding stale repository guidance to start from `main`. At dispatch, the parent reported that the then-stable main and beta had no merge base. A later read-only precommit check found local `main` at `33907b5deff71d532d14be990f849c8bfa1519b3`, now an ancestor of beta (merge-base exit 0). That shared ref changed outside this task; this subagent made no main or beta ref changes. The owned feature base and explicit beta target remain unchanged. No nested reviewer or Kanban changes by this subagent.

## Implemented contract

- `_fetch_direct_series_markets` lazily yields listings across URL-encoded cursor pages, including empty pages with continuation, until cursor exhaustion or the caller's accepted-market quota stops consumption. There is no ten-contract or ten-page series cap.
- Only one runtime caller exists, in `get_markets_direct`; repository search found no other private-helper consumers. Its private return changes from list to iterator. Public `get_markets_direct(limit, page_size, max_pages)` stays unchanged.
- Series page size is the requested `page_size`, capped by remaining initial global quota and clamped to 1..1000 at the HTTP boundary. No subsequent page, later series, or generic request is issued after the accepted quota fills.
- Existing quote conversion, metadata, route/group gates and shared freshness rules remain intact. Freshness is now applied before counting accepted rows in both series and generic direct passes. Only accepted IDs enter the shared dedup set; duplicates across pages, series and generic listings do not use quota.
- Repeated/cycling cursors terminate with `DirectMarketFetchUnavailable`, not a loop or a successful incomplete result. Failed requests, non-200 series responses, malformed listing envelopes and decode failures also raise that existing exception type. Diagnostics identify series, page, fetched count and failure reason; the caller logs its accepted count. Causes are chained, opaque cursor values and response bodies are not logged.
- Existing shared throttle/retry helper and timeouts are reused. Generic fallback retains its explicit `max_pages` bound and pre-existing cursor/error policy; it is not redesigned here.
- Startup series discovery, registry coverage, station mapping, aliases, date/safeguard logic, raw evidence, live configuration and services are unchanged. Reaching a global quota is not proof of exhaustive exchange-wide discovery.

## Evidence and runnable tests

Interpreter used: `/home/ryushe/projects/prediction-bot-beta-integration/.venv/bin/python` (Python 3.13). Source-path check resolved `bot.exchanges.kalshi` inside the owned worktree, not the environment's editable checkout. A fresh-worktree mtime screen flagged the manifest; installed versions matched all exact pins and minimum constraints in `requirements.txt`, so no reinstall or manifest edit was needed.

Run from the owned worktree, with its source and tests first:

```bash
PY=/home/ryushe/projects/prediction-bot-beta-integration/.venv/bin/python
export PYTHONPATH="$PWD:$PWD/tests"
"$PY" -B -m unittest test_kalshi_direct -v
"$PY" -B -m unittest \
  test_kalshi_direct test_kalshi_quotes test_prediction_lab_collect \
  test_collector_weather_identity test_weather_identity_reconciliation \
  test_weather_legacy_city_identity test_weather_identity_geography_guard \
  test_weather_lab_location_identity test_market_router -v
git diff --check
```

These tests use synthetic transports and temporary stores. The new retry tests execute the actual shared HTTP retry helper with mocked HTTP responses/timeouts and sleep/throttle hooks; no account credentials or live calls are used.

Receipts:

- **RED, before pagination implementation:** `test_series_pagination_exhausts_more_than_ten_pages_below_global_limit` failed with 17 of 27 expected IDs absent (only ten returned).
- **GREEN:** that test traverses 14 series pages at size two, returns all 27 contracts below the global limit, then performs the independently bounded generic fallback even with `max_pages=1`.
- **RED, before quota-order change:** `test_rejected_and_duplicate_rows_do_not_consume_global_quota` failed in both series and generic subtests, returning zero instead of two later eligible IDs. Both now pass, covering closed/missing-close/stale/quote-ineligible/noneligible rows, duplicate rows and immediate stop at the global quota.
- **RED, before failure/cursor repair:** first/later-page failure diagnostics and repeated/cycling-cursor tests failed as expected (14 failing subtests in that original iteration). They now pass; coverage additionally checks malformed cursor types, causal exceptions and accepted-count logs.
- **Final direct/continuation/retry tests:** 15 passed, zero failures/errors.
- **Final focused collector/quote/identity/router suite:** 136 passed, zero failures/errors/skips, 12.897 seconds under a task-local Python audit guard forbidding sockets and subprocesses and restricting writes to the owned worktree and scratch directory. Expected synthetic fault logs are emitted by existing failure-path tests.
- Initial guarded suite invocation included a nonexistent `test_market_classification` module and failed import; removing that runner-only typo yielded the full declared 136-test green receipt. No product change was made for it.
- `git diff --check` passed.

Task-local guard/receipt (not product files): `/home/ryushe/.hermes/cache/scratch/kalshi-pagination-t_d694dfa0/verify_offline.py` and `focused-final.log`. Scratch can expire; the committed tests and commands above are the durable verification surface.

### Recoverable RED reproduction

Both primary regressions can be replayed against the immutable base without modifying source files. In a fresh Python process from this branch, import `bot.exchanges.kalshi`, obtain `git show 46806cf736917d3c6106c4766aa5e94f35f66a8d:bot/exchanges/kalshi.py`, execute that source in the module's namespace, and then load these current test names:

- `test_kalshi_direct.KalshiDirectMarketTests.test_series_pagination_exhausts_more_than_ten_pages_below_global_limit`
- `test_kalshi_direct.KalshiDirectMarketTests.test_rejected_and_duplicate_rows_do_not_consume_global_quota`

Actually replayed: two tests, three expected assertion failures (one pagination plus two freshness subtests), zero errors. Thus baseline source and regression source are recoverable from branch history, not only a dirty worktree.

### Optional retained-catalog replay — performed offline

Read-only input root:
`/mnt/data-collection/prediction-bot/data/derived_reports/weather_identity_repair_t_ca47d502/current_market_validation_t_1de472a9`.

Used `per_market_validation.v2.jsonl`'s `daily_weather_scope` IDs to select 576 original rows from `market_observations.jsonl`, preserving per-series order across 48 series. Replayed each as mocked ten-row cursor pages, with the existing gates and a fixed adapter clock of `2026-10-23T03:36:34+00:00` (catalog-time eligibility, not today's admission). `limit=1000`, `page_size=10`, `max_pages=1` returned all **539 eligible contracts**, versus **455 eligible in the first ten per series**, recovering **84**. It used 96 series requests plus one successful-empty generic request. The other 37 contracts stayed quote-ineligible. This is a frozen public-catalog transport simulation, not an authenticated live rerun or evidence that all 576 are collectible.

Input digests were checked unchanged before/after:

- `market_observations.jsonl`: `fc554bc5f03072169f04bbefdeabe22802f6ece147a2d87dd6209f49b49f4062`
- `per_market_validation.v2.jsonl`: `e37ac4474c7aa364cf857818f8bd8b4ce356179a2917df91a0e6a8b5028c43f5`

Task-local probe: `/home/ryushe/.hermes/cache/scratch/kalshi-pagination-t_d694dfa0/replay_catalog.py`; no report, raw file, receipt, capture store or index in that input root was written.

## Deferred checks and residuals

- **Full suite on initial reviewed tip:** parent supplied the existing frozen-source Landlock/seccomp runner after Bubblewrap namespace admission failed. Exact `b1d64a7e4a2ed3bd898a3d66dd99410ddb4e95ed`, tree `99df1d389e7b58b36884b9c229c4cf35fec00ef1`: 1,836 unique tests ran, zero failures/errors, runtime-local config checks skipped and one real-archive smoke deliberately excluded. All 478 tracked source files remained unchanged; kernel source-write/connect/sendto denial probes passed. Receipts: `/mnt/data-collection/prediction-bot/data/derived_reports/weather_identity_repair_t_ca47d502/pagination_t_d694dfa0_feature/{verification.json,full-receipt.json}`. This supersedes the initial missing broad-regression evidence, not the activation boundary. The corrected candidate needs its own full-suite receipt.
- No live acceptance test was performed or authorized. Parent review must reconcile the exact candidate range; station/provider availability and completeness of the discovered startup series remain out of scope.
- Series traversal intentionally has no arbitrary page cap: it stops at quota, exhaustion, repeated cursor or request failure. An API returning indefinitely unique continuation tokens remains bounded only by those operational conditions, not `max_pages` (which continues to govern generic fallback only).

## Interruption / resume handoff and decision gates

- **Implementation checkpoint:** `70ba66e011f2ec614f0f06668d831ece7825cb68` (initial pagination implementation `2eec119aa5cf1e34f8185a4ccba72d1e3a7e9995` plus the cursor correction). This checkpoint includes code, regression and the activation hold. A later dossier-only handoff commit pins this immutable ID; executable code/tests are unchanged by that handoff.
- **Exact resume point:** obtain operator decision on the shared beta execution/activation boundary. If authorized to integrate, fetch/reconcile beta, verify source/test blobs still match reviewed `3259aae38ba780a35d90cefd47b69ea9c92d9342`, exclude this branch-local dossier from integration, and verify the combined target before any merge/push. Source/tests are reviewed and tested; don't mistake the remaining runtime hold for missing code evidence.
- **Expected working-tree state at handoff:** clean after implementation plus dossier-only checkpoint commit.
- **Integration:** parent-owned review required before any beta merge; remove this temporary dossier from the integration target if accepted, preserving branch history.
- **Activation / cohort gate:** not requested; no restart, collector capture, configuration change or new cohort.
- **Promotion gate:** no stable-main action authorized.

## Decision record

- 2026-10-22 PDT — Implemented and locally exercised only direct per-series pagination and its accepted-quota boundary. Await parent independent review; no merge, push, restart or activation.

## Parent review follow-up and activation hold

- Independent review `deleg_691e70fa` approved the initial exact range, conditional on the parent's full gate; that gate passed as recorded above. Independent reviewer reran 136 focused tests and found no blocking pagination defect.
- Parent elected to correct the low-priority finding rather than narrow the malformed-cursor contract: absent/null/empty-string cursor means exhaustion; falsy non-string values `0`, `False`, `[]`, `{}` now raise incomplete-discovery error. The added regression first failed in all four invalid subtests with no errors, then the corrected 137-test focused suite passed with zero failures/errors/skips. Durable logs: `current_market_validation_t_1de472a9/pagination_parent/{cursor-red.log,focused-cursor-fixed.log}` beneath the derived weather-identity report root. No quote, station, metadata or forecast-safeguard changes were added.
- Initial review's clock-movement residual remains: accepted contracts may close before the final freshness pass, so returning fewer than the requested quota is not proof of missing cursor traversal; no final-quota guarantee under advancing time is claimed.
- **Activation hold:** service readback shows `Restart=on-failure` and automatic retries with an accounting-integrity failure. Its configured `PROJECT_ROOT` is `/home/ryushe/projects/prediction-bot-beta-integration`, the actual beta integration worktree. A merge there could be loaded by an automatic retry even without a manual restart. Do not merge into that execution path until the operator authorizes quiescing/rollout or it is independently proven nonexecuting. This task has not stopped/restarted the unit or modified populated accounting, raw data, unit or configs. The separate integrity failure is `capture directory changed outside accounting lock`, not a pagination failure.
- Final independent review `deleg_3fcf641b` **APPROVED** exact `b1d64a7e4a2ed3bd898a3d66dd99410ddb4e95ed..3259aae38ba780a35d90cefd47b69ea9c92d9342`, tree `583e8a47f92f9e9ae1471a847545178691cfcb02`. Fresh independent focused suite: 137 tests, zero failures/errors/skips. Parent verified the log SHA-256 `06975d8cdfcefa809da24b30cb6517a52e03c8323f65593364f10789dac01ec2` and preserved guard/log in durable `current_market_validation_t_1de472a9/pagination_parent/{independent-final-guard.py,independent-final-focused.log}` rather than relying on expiring scratch.
- Corrected-candidate full gate **PASSED** exact `3259aae38ba780a35d90cefd47b69ea9c92d9342`: 1,837 unique tests run, zero failures/errors, seven runtime-local config skip entries and one real-archive smoke excluded. All 478 tracked source files unchanged. Durable report root `/mnt/data-collection/prediction-bot/data/derived_reports/weather_identity_repair_t_ca47d502/pagination_t_d694dfa0_corrected/` contains `verification.json`, `full-receipt.json`, source manifest, confinement runner and log. Read the explicit exclusions; this is not an unfiltered live/archive acceptance claim.
- Parent reran the saved-catalog replay against that exact frozen corrected source: 539 eligible of 576 contracts, recovering the same 84 beyond the first-ten baseline of 455; both input hashes unchanged. Durable receipt `current_market_validation_t_1de472a9/pagination_parent/catalog-replay-corrected.json` and parameterized `replay_catalog.py CANDIDATE_SOURCE_ROOT` are retained.
- No authorization was received from the quiesce/diagnose prompt. Keep beta, the service and accounting untouched. Code and handoff are committed locally; nothing merged, pushed or activated. Resume requires operator decision, not another unattended restart or populated-accounting reset.
