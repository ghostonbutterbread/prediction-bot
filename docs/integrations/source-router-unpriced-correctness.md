# Source Router unpriced correctness admission

- **Status:** feature — isolated fix; review pending
- **Owner:** Hermes
- **Branch / owning ref:** `fix/source-router-unpriced-correctness`
- **Worktree:** `/home/ryushe/worktrees/prediction-bot-source-router-unpriced-correctness`
- **Base commit:** `14982f867bc324aae21bb1c2c8e396fb176b4dfe`
- **Intended integration target:** `beta` (handoff only; no merge/push/runtime authorized)
- **Last updated:** 2026-10-05
- **Latest immutable recovery checkpoint:** `73918f499e7fa89b7eb060c0df1e1f2681f8b2e9`
- **Feature implementation commits:** `28e99bc2d18f5981ec5a614726cf752d48a648c9`, `9b6d1880d16c3239f91c79297a2e5942a0dfe1e7`, `efb4a02ec5884572aa1aa9128b972cdc53b24616`, `03d24ab8727805434d66aeeccf190b741e4cd8ac`, `73918f499e7fa89b7eb060c0df1e1f2681f8b2e9`

## Intent and implemented contract

Repair the existing strict settled forecast-correctness admission at the Source Router selector, not the edge evaluator or trade gate. A hash/proof-qualified forecast with an authoritative independent YES/NO outcome, matching directional correctness, and settlement strictly before a decision may rank a source even if its historical selected-side price is absent. Edge/PnL fields stay null and `eligible_for_edge_validation` stays false. Neither historical price nor realized outcome is available to the sealed decision. The candidate's own selected-side decision price and edge threshold remain unchanged. Collapse repeated observations by source + physical event ticker + contract shape before sample thresholds; market-only identities remain contract-only fallbacks.

No collector, raw archive, configuration, wallet, service, or runtime changes. Historical replay remains a diagnostic, not forward promotion evidence.

## Evidence and review

- RED: `PYTHONPATH="$PWD" python3 -m unittest tests.test_collector_source_router_replay.CollectorSourceRouterReplayTests.test_unpriced_strict_settled_history_routes_without_creating_edge_or_pnl tests.test_collector_source_router_replay.CollectorSourceRouterReplayTests.test_unpriced_history_rejects_missing_proof_conflicts_and_nonprior_settlements -v` failed on price-only selector rejection.
- RED: event collapse across two same-event market thresholds, and priced marker without independent outcome, failed before implementation.
- GREEN focused after review-blocker repairs: `PYTHONPATH="$PWD" /home/ryushe/projects/prediction-bot/.venv/bin/python -m unittest tests.test_weather_source_router tests.test_collector_source_router_replay -q` — 60 tests OK. RED cases cover contradictory history and final exact resolutions (including a BUY that previously fabricated correctness/PnL), progressive/direct mismatched forecast sides, and same-market target/event-label variation.
- Full suite: initial interpreter lacked `rich` and `kalshi_python_sync` (1256 tests, 22 import errors). Latest source tree with provisioned project interpreter and this worktree first on `PYTHONPATH`: `PYTHONPATH="$PWD" /home/ryushe/projects/prediction-bot/.venv/bin/python -m unittest discover -s tests -q` — 1382 tests OK, 7 skipped (66.130s). Go `GOROOT` warning from a subprocess was nonfatal.
- `git diff --check` and `git diff --cached --check` passed before committing implementation.
- First independent read-only review found history-only conflicting outcomes and post-settlement observations; second found ordinary/direct/collector conflicts; third found exchange aliases and a VOID lookup. Fourth review of `efb4a02` found conflicting manifest resolution/root/nested outcome assertions, mismatched progressive forecast sides, and one market split by different targets. Fifth review confirmed those repairs but found the separate exact-outcome index still accepted conflicting aliases and could fabricate resolved correctness and PnL on a newly routed BUY. RED→GREEN end-to-end regression repairs that gate too. Fresh independent review of the corrected tree remains pending; no integration decision delegated or implied.
- Ancestry: branch started at local `beta` commit `14982f867bc324aae21bb1c2c8e396fb176b4dfe`.
- Published 168/168 strict source rows remain a frozen diagnostic with at most two independent samples per source/slice; production 100-sample threshold unchanged. No published generation was rewritten or used to claim a BUY/PnL improvement.

## Blockers / deferred work

- **Verification:** full isolated suite passed on the latest implementation tree; `GOROOT` emitted a nonfatal subprocess warning. Synthetic fixtures prove the admission boundary, not the published generation's hash/readback or forward readiness. Fresh independent review remains pending.
- **Review/merge:** independent review of this feature and any staged-tree integration check belong to the next authorized owner; no merge or push in this task.
- **Forward evidence:** event-disjoint, independently resolved paper cohort required before activating/promoting any trading behavior.

## Interruption / resume handoff

- **Owning feature branch/ref:** `fix/source-router-unpriced-correctness`
- **Latest immutable recovery checkpoint:** `73918f499e7fa89b7eb060c0df1e1f2681f8b2e9` (fifth implementation); branch tip may also contain a later dossier-only handoff commit.
- **Feature implementation commits:** `28e99bc2d18f5981ec5a614726cf752d48a648c9`, `9b6d1880d16c3239f91c79297a2e5942a0dfe1e7`, `efb4a02ec5884572aa1aa9128b972cdc53b24616`, `03d24ab8727805434d66aeeccf190b741e4cd8ac`, `73918f499e7fa89b7eb060c0df1e1f2681f8b2e9`.
- **Exact resume point:** independent read-only review of `73918f499e7fa89b7eb060c0df1e1f2681f8b2e9`; then separately authorized beta staged-tree reconciliation, excluding this branch-local dossier.
- **Working-tree state:** clean after this dossier handoff commit.

## Decision gates

- **Integration:** committed fix + tests + independent review on target tree, separately authorized.
- **Activation/cohort:** separately authorized forward consumer validation; no runtime change here.
- **Promotion:** fresh settled forward-paper evidence, not historical diagnostic replay.

## Decision record

- 2026-10-04 — isolated TDD repair under strict history and event-independence boundary; no merge/push/runtime.
- 2026-10-04 — corrected two additional outcome-conflict bypasses found by independent review; RED→GREEN regressions and complete isolated suite; third read-only review pending; still no merge/push/runtime.
- 2026-10-04 — corrected exchange outcome aliases and VOID-lookup bypass from third review; full isolated suite passes; next independent review pending; still no merge/push/runtime.
- 2026-10-05 — closed fourth-review outcome/side/independence blockers with RED→GREEN tests and full suite; independent review of corrected tree pending; still no merge/push/runtime.
- 2026-10-05 — fifth review found contradictory final outcomes could yield fabricated correctness/PnL; exact resolver now rejects such receipts. Full isolated suite passes; no merge/push/runtime.
