# Strict source-observation identity integration dossier

- **Status:** review-ready
- **Owner:** Hermes Agent
- **Branch:** `fix/strict-source-observation-identity`
- **Base commit:** `3712485` (`beta`)
- **Intended integration target:** `beta`
- **Last updated:** 2026-08-26
- **Inspiration / canonical references:** `bot/weather/source_observation_ledger.py`; strict collector cohort `forward_20260823T2025Z_strict_source_router`

## Intent

Fix a real strict-source-ledger failure: distinct recorded source records could produce the same observation ID when their normalized scoring fields matched, causing materialization to abort with a conflicting-duplicate error. Preserve every immutable source record for later repeat-poll collapse without weakening source-target or outcome checks.

## Implemented contract

`source_observation_id` now incorporates the canonical hash of the complete recorded source payload. Different source records are retained as distinct observations even when their normalized source payload is equal. Exact duplicate records remain deduplicable, and the existing strict-history collapse remains responsible for choosing one independent representative. No raw collector input, resolver input, wallet, or runtime configuration changes.

## Evidence and review

- Tests and commands: `PYTHONPATH=. python3 -m unittest tests.test_source_observation_ledger tests.test_collector_source_router_replay tests.test_replay_outcome_binding` — 44 tests passed.
- Independent review: pending.
- Replay/cohort/fixture evidence: the active-cohort smoke materialized 776 pending source observations without the former conflict; the selected newest 100 replay inputs have zero exact finalized bindings, so it correctly produced zero settled rows.
- Merge/ancestry evidence: branch created from clean `beta` at `3712485`; `git diff --check` passed.

## Blockers and deferred work

- **Missing test or evidence:** full-suite dependency coverage and a settled, exact-bound active-cohort source-history sample.
- **Command / fixture / environment needed:** install/provide `kalshi_python_sync` for `tests/test_kalshi_direct.py`; wait for matching finalized resolutions for the selected current snapshot identities.
- **Trigger to run it:** before beta integration and after an eligible cohort receives matching finalized resolution rows.
- **Why it blocks integration, activation, or promotion:** the identity fix is locally verified, but it must not activate or promote source routing; current smoke input is intentionally unbound.
- **Next smallest completion step / successor reference:** independent review, then merge the fix into beta only if review accepts it.

## Decision gates

- **Integration gate:** independent review accepts the focused diff and tests.
- **Activation / cohort gate:** a fresh strict cohort has exact-bound outcomes and at least the configured 100 prior eligible observations per scoring unit.
- **Promotion gate:** forward paper overlap, independent outcomes, and execution/price evidence; this patch alone supplies none.

## Decision record

- 2026-08-26 — created after reproducing source-observation ID conflict on current collector-derived replay inputs.
- 2026-08-26 — added source-record provenance to the identity and a regression test; focused verification passed.
- 2026-08-26 — independent read-only review exercised 70 related tests without a code finding; Ryushe directed local beta integration.
