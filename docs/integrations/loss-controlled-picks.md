# Loss-controlled picks integration dossier

- **Status:** experiment
- **Owner:** Hermes, prediction-bot Kanban `t_7cb2e9fd`
- **Branch:** `feat/loss-controlled-picks`
- **Base commit:** `f4fa746e0484c083ee0ea5c019caae33058bb925`
- **Intended integration target:** `beta` (local integration lane; not runtime)
- **Last updated:** 2026-09-27
- **Owning feature branch/ref:** `feat/loss-controlled-picks`
- **Latest immutable recovery checkpoint:** `2c6e18de2d19b15323e7de02b5a752d01b236fcd` (implementation and dossier; a later dossier-only handoff commit may be the feature tip)
- **Feature implementation commit(s):** `2c6e18de2d19b15323e7de02b5a752d01b236fcd`
- **Inspiration / canonical references:** Ryu's Discord thread `1553590583275298928`; existing disabled `shadow_fee_aware_edge_floor`; fresh-quote probe `data/derived_reports/price_normalization_20260921T044200Z/fresh-quote-probe-20260925T052252.954042_0000.json`.

## Intent

Test a single source-independent pick-selection hypothesis: retain stable recorded BUYs only when a selected-side decision-time ask is at least $0.20 and the estimated fee-aware net edge clears $0.03. Reuse the existing fee-aware lane evaluator and keep Source Router out of the selector; a later augmentation is separate work. Compare losses avoided against winning picks discarded and control/candidate quote-based fixed-stake proxy economics on the *same fixed snapshot*. Do not tune or claim an optimal threshold from the evaluation cohort.

## Implemented contract

A new disabled `shadow_fee_aware_loss_guard` extends existing stable-BUY selectivity: selected-side recorded order-book ask at least $0.20 and fee-aware estimated net edge at least $0.03, with the worse recorded estimate used when available; it cannot create or reverse a BUY. The forward lane reads the structured book envelope on the shared candidate, requires `source: book`, matching market identity and book observation no later than candidate/decision time, and never treats a quote or estimated fill as a resolved fill/P&L. The paired probe uses the September 25 original fixed 393 raw locators and verifies each selected raw-line hash, identity, matching shared-candidate book provenance and immutable decision-time action/probability. It seals actions before opening the separate strict-resolution ledger, binds only finalized exact-market outcomes settled after each decision, and leaves unknown/conflicting/VOID receipts out of metrics. Artifacts are written only to a fresh derived reports path. This is a **recorded-decision diagnostic**, not independent re-execution of the original strategy, an executable fill/P&L claim, synthetic-wallet parity, or promotion evidence.

## Evidence and review

- Focused: `PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 python -B -m unittest tests.test_paper_shadow_lanes tests.test_paired_loss_guard_probe -q` → 80 tests OK, 1 skipped after provenance/accounting fixes. Full isolated: `PYTHONPATH=$PWD PYTHONDONTWRITEBYTECODE=1 /mnt/data-collection/prediction-bot/.venv/bin/python -B -m unittest discover -s tests -q` → 1,216 tests OK, 7 skipped in 65.055s. `git diff --check` clean. A prior full rerun timed out due concurrent suites and its owned process was terminated; this later isolated run supersedes it.
- Independent review: first read-only review held integration for flattened reference asks and quote-as-fill resolution accounting; added failing regressions and fixed both. Fresh read-only re-review of committed `f4fa746..8bdad61` approved **disabled-code beta integration consideration**, found no remaining blocker, reran 80 focused and 1,216 full tests. Activation/promotion remain held pending fresh event-disjoint forward-paper evidence.
- Replay/cohort/fixture evidence: `/mnt/data-collection/prediction-bot/data/derived_reports/loss_guard_probe_20260927_v3/` read back with 393 paired rows and verified derived hashes. Control: 8 winning/41 losing resolved BUYs, 49 priced. Candidate: 8 winning/2 losing resolved BUYs, 10 priced. 40 of 393 markets were not finalized; 7 recorded BUYs unresolved. Accepted BUYs span 8 events versus 26 events for resolved control BUYs; correlated strikes and one cohort invalidate independent-sample inference. Fixed $10 per BUY, fee-free *ask-quote proxy* P&L: control -$304.9636, candidate +$85.0364. These are not realizable fills, wallet results, or forward evidence. The $0.20 cutoff was selected after inspecting this cohort: the observed winner retention is explicitly in-sample/selection-biased, not generalizable or optimal.
- Merge/ancestry evidence: branch created from local beta `f4fa746` after fetching `origin/beta`; local beta was clean and 21 commits ahead of remote at creation.

## Reproduction / script map

- `scripts/paired_loss_guard_probe.py` — bounded, non-mutating fixed-locator recorded-decision diagnostic; requires `--probe-manifest` (frozen raw-line locators), `--strict-resolutions` (separate authoritative ledger), and a *new* `--output-dir` beneath collector `data/derived_reports`. This is not a live runner. The reproducible September 25 input receipt and exact output path are above; invoke `python -B scripts/paired_loss_guard_probe.py --help` for flags. Do not reuse the historical cohort to tune another threshold and call it validation.

## Blockers and deferred work

- **Missing test/evidence:** independently re-executed full strategy decisions and out-of-sample event-disjoint, forward settled paper comparison. **Needed:** new strict decision-time source/price and config provenance plus independent later cohort. **Trigger:** after the paired diagnostic and collector/replay producer compatibility are verified. **Why:** recorded baseline actions and asks cannot prove improved production pick quality or executable profitability.
- **Missing test/evidence:** depth/fill and stateful wallet/fee/risk parity. **Needed:** exact executable quote/fill provenance and a later independently resolved overlapping cohort. **Trigger:** before any activation or promotion; keep code disabled meanwhile.

## Interruption / resume handoff

- **Owning feature branch/ref:** `feat/loss-controlled-picks`
- **Latest immutable recovery checkpoint:** `2c6e18de2d19b15323e7de02b5a752d01b236fcd` (implementation and dossier; a later dossier-only handoff commit may be the feature tip)
- **Feature implementation commit(s):** `2c6e18de2d19b15323e7de02b5a752d01b236fcd`
- **Exact resume point:** perform a non-committing beta compatibility merge, run beta-worktree focused/full tests, abort the preflight, then merge the reviewed disabled code only if the target stays clean. Do not activate without a separately authorized fresh, event-disjoint forward-paper cohort.
- **Working-tree state at handoff:** clean committed feature required before review; no raw/runtime/config mutation.

## Decision gates

- **Integration gate:** focused/full tests, bounded real-data replay with explicit coverage and adverse cases, independent review, reconciled beta diff and clean target; no behavior enabled by default.
- **Activation / cohort gate:** separately authorized fresh forward-paper control/candidate comparison on exact shared snapshots; no live orders.
- **Promotion gate:** independently resolved full-overlap, executable-price and wallet/risk evidence with event-disjoint validation; historical diagnostic alone insufficient.

## Decision record

- 2026-09-27 — First review held on two price/P&L provenance defects; fixed test-first, independently re-reviewed and approved for disabled-code beta consideration only. Local beta `f4fa746` clean, `origin/beta` ancestor (beta ahead 21). No activation/promotion; next noncommitting beta compatibility test.
