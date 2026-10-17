# Source Router unpriced correctness admission

- **Status:** feature — corrected code-only beta staged tree accepted; local beta merge authorized, not yet performed
- **Owner:** Hermes
- **Branch / owning ref:** `fix/source-router-unpriced-correctness`
- **Worktree:** `/home/ryushe/worktrees/prediction-bot-source-router-unpriced-correctness`
- **Base commit:** `14982f867bc324aae21bb1c2c8e396fb176b4dfe`
- **Intended integration target:** `beta` (local merge after exact-tree review; no push/runtime authorized by this task)
- **Last updated:** 2026-10-17
- **Latest immutable recovery checkpoint:** `80a6d47147393d2e2fc7ec20615b9f33bdb4bd75`
- **Feature implementation commits:** `28e99bc2d18f5981ec5a614726cf752d48a648c9`, `9b6d1880d16c3239f91c79297a2e5942a0dfe1e7`, `efb4a02ec5884572aa1aa9128b972cdc53b24616`, `03d24ab8727805434d66aeeccf190b741e4cd8ac`, `73918f499e7fa89b7eb060c0df1e1f2681f8b2e9`, `d3bd34590e92ebfcfb5b11f0704750640ec16ffe`, `80a6d47147393d2e2fc7ec20615b9f33bdb4bd75`

## Intent and implemented contract

Repair the existing strict settled forecast-correctness admission at the Source Router selector, not the edge evaluator or trade gate. A hash/proof-qualified forecast with an authoritative independent YES/NO outcome, matching directional correctness, and settlement strictly before a decision may rank a source even if its historical selected-side price is absent. Edge/PnL fields stay null and `eligible_for_edge_validation` stays false. Neither historical price nor realized outcome is available to the sealed decision. The candidate's own selected-side decision price and edge threshold remain unchanged. Collapse repeated observations by source + physical event ticker + contract shape before sample thresholds; market-only identities remain contract-only fallbacks.

The manifest join now rejects contradictory recorded-versus-strict outcomes before projecting strict authority. Exact final resolution requires settlement strictly after the sealed decision observation before awarding correctness or PnL. No collector, raw archive, configuration, wallet, service, or runtime changes. Historical replay remains a diagnostic, not forward promotion evidence.

## Evidence and review

- RED: `PYTHONPATH="$PWD" python3 -m unittest tests.test_collector_source_router_replay.CollectorSourceRouterReplayTests.test_unpriced_strict_settled_history_routes_without_creating_edge_or_pnl tests.test_collector_source_router_replay.CollectorSourceRouterReplayTests.test_unpriced_history_rejects_missing_proof_conflicts_and_nonprior_settlements -v` failed on price-only selector rejection.
- RED: event collapse across two same-event market thresholds, and priced marker without independent outcome, failed before implementation.
- GREEN focused after review-blocker repairs: `PYTHONPATH="$PWD" /home/ryushe/projects/prediction-bot/.venv/bin/python -m unittest tests.test_weather_source_router tests.test_collector_source_router_replay -q` — 61 tests OK. RED cases cover contradictory history and final exact resolutions, progressive/direct mismatched forecast sides, and event links arriving on later valid polls of the same market (including transitive conflicting labels).
- Full suite: initial interpreter lacked `rich` and `kalshi_python_sync` (1256 tests, 22 import errors). Latest source tree with provisioned project interpreter and this worktree first on `PYTHONPATH`: `PYTHONPATH="$PWD" /home/ryushe/projects/prediction-bot/.venv/bin/python -m unittest discover -s tests -q` — 1383 tests OK, 7 skipped (349.007s). Initial 300s run timed out amid concurrent filesystem I/O; 600s isolated retry completed. Go `GOROOT` warning from a subprocess was nonfatal.
- `git diff --check` and `git diff --cached --check` passed before committing implementation.
- First six independent read-only reviews identified and drove RED→GREEN repairs of outcome conflicts, settlement/observation chronology, implied-side conflicts, manifest and final exact-resolution claims, and market/event independence. Seventh read-only review of `d3bd34590e92ebfcfb5b11f0704750640ec16ffe` found **no blockers** in targeted pure-function probes of transitive late/conflicting event links, earliest valid forecast, proof/chronology, unpriced edge/PnL separation, and current price/edge gates. This is feature-tree acceptance only, not a beta staged-tree review or activation decision.
- First beta preflight from `de978fcf1ae2dcc85dfd9a4f66f47eb3f206f929` yielded code-only tree `c87b31bce0a3053d9b28e67594661fee64f8ead8` (dossier excluded), focused 31+30 OK and full beta Python 3.13 suite 1443 OK/7 skipped. Independent exact-tree review **blocked** on manifest join overwriting contradictory recorded outcomes and pre-decision/equal final settlement awarding correctness/PnL. Aborted preflight without merging.
- Review-blocker repair on this isolated feature: RED manifest conflict fixture emitted BUY_YES instead of SKIP; RED early/equal final-outcome fixture resolved two decisions instead of none. GREEN both targeted tests, 63 focused tests, and full isolated Python 3.14 suite 1385 OK/7 skipped. The subsequent combined-tree tests/review are recorded below.
- Corrected second beta preflight from `de978fcf1ae2dcc85dfd9a4f66f47eb3f206f929`: code-only tree `1510bbda5a759d5e745dabf2f725e229b296b4fd`; focused 31+32 OK, full beta Python 3.13 suite 1445 OK/7 skipped, `git diff --cached --check` clean. Fresh independent read-only review **ACCEPTED this exact staged tree**, independently checked both previous blockers, strict aliases/VOID, chronology, unpriced correctness vs PnL, current price/edge, and targeted composed/forward tests. This is beta compatibility evidence only, not forward-paper parity or promotion evidence.
- Ancestry: branch started at local `beta` commit `14982f867bc324aae21bb1c2c8e396fb176b4dfe`.
- Published 168/168 strict source rows remain a frozen diagnostic with at most two independent samples per source/slice; production 100-sample threshold unchanged. No published generation was rewritten or used to claim a BUY/PnL improvement.

## Blockers / deferred work

- **Verification:** full isolated suite and independent feature-tree read-only review passed; `GOROOT` emitted a nonfatal subprocess warning. Synthetic fixtures do not prove published-generation hash/readback or forward readiness.
- **Review/merge:** exact code-only staged tree is accepted; reproduce the identical tree in the clean local beta worktree, rerun focused/full tests and merge only on matching tree identity. No push or runtime activation here.
- **Forward evidence:** event-disjoint, independently resolved paper cohort required before activating/promoting any trading behavior.

## Interruption / resume handoff

- **Owning feature branch/ref:** `fix/source-router-unpriced-correctness`
- **Latest immutable recovery checkpoint:** `80a6d47147393d2e2fc7ec20615b9f33bdb4bd75` (seventh implementation); branch tip may also contain a later dossier-only handoff commit.
- **Feature implementation commits:** `28e99bc2d18f5981ec5a614726cf752d48a648c9`, `9b6d1880d16c3239f91c79297a2e5942a0dfe1e7`, `efb4a02ec5884572aa1aa9128b972cdc53b24616`, `03d24ab8727805434d66aeeccf190b741e4cd8ac`, `73918f499e7fa89b7eb060c0df1e1f2681f8b2e9`, `d3bd34590e92ebfcfb5b11f0704750640ec16ffe`, `80a6d47147393d2e2fc7ec20615b9f33bdb4bd75`.
- **Exact resume point:** abort the detached preflight, commit this dossier-only acceptance, recreate the code-only merge in clean beta at `de978fcf1ae2dcc85dfd9a4f66f47eb3f206f929`, require staged tree `1510bbda5a759d5e745dabf2f725e229b296b4fd`, rerun focused/full tests, then commit locally. Runtime/cohort validation remains separate.
- **Working-tree state:** implementation committed at `80a6d47147393d2e2fc7ec20615b9f33bdb4bd75`; clean after this dossier-only acceptance commit.

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
- 2026-10-05 — sixth review found a later event label lost at market collapse; connected valid market/event identities before earliest-forecast selection. Full isolated suite passes; no merge/push/runtime.
- 2026-10-05 — seventh independent feature-tree read-only review found no blockers; retained isolated committed handoff, with beta integration and runtime explicitly deferred.
- 2026-10-17 — first beta preflight passed tests but exact-tree review blocked two outcome-integrity gaps; returned repairs to the isolated feature with RED→GREEN regressions and full suite. No beta merge/push/runtime.
- 2026-10-17 — corrected code-only beta preflight `1510bbda5a759d5e745dabf2f725e229b296b4fd` passed full suite and fresh independent exact-tree review. Local beta merge may proceed only with the same staged tree; no push/runtime.
