# Live Parity Implementation Brief

## Objective

Reflect the repo's current parity state accurately, then define the next concrete implementation pass.

Parity mode itself is no longer the main gap. The stronger next move is to:
1. normalize the shared execution/audit row contract
2. harden the existing parity diff/report surface
3. use that visibility to drive live lifecycle hardening

## Status Update

The execution/audit contract pass is now substantially in place.

That means this brief should now be read mainly as:
- the record of why the contract/parity cleanup mattered
- the bridge into the next live-readiness pass

The concrete next-pass live spec now lives in:
- `docs/LIVE_LIFECYCLE_HARDENING_SPEC.md`

## Architectural Rule

If a behavior affects decision inputs in both paper and live, it must be moved into shared code before either mode depends on it.

Use this rule throughout implementation:
- **Shared core**: decision logic, normalized decision inputs, common reasoning fields
- **Adapters**: environment-specific state gathering, exchange/API calls, simulated fills, live order placement
- **Reports**: should consume a canonical row contract rather than reverse-engineering adapter-specific shapes
- **Do not** duplicate execution-time price normalization in paper and live

## Current Repo State

What already exists:
- Shared decision logic exists in `bot/shared_core/decision.py::build_trade_decision()`
- Shared execution snapshot normalization exists and is already used in live (`bot/live_execution.py`) and paper-facing paths (`bot/paper_adapters.py` imports `build_execution_snapshot`)
- Paper parity metadata is already flowing into persisted rows in some form
- A first normalization/report layer already exists in `bot/parity_audit.py`
- A first local viewer already exists in `scripts/parity_viewer.py`
- Shared audit/accounting helpers already exist in `bot/trade_audit.py`
- There are already parity-focused tests in `tests/test_parity_audit.py`, `tests/test_parity_proofs.py`, `tests/test_recovery_parity.py`, and related files

What is still weak:
- the repo does **not** yet enforce one canonical write-time execution/audit row schema across paper and live
- `bot/parity_audit.py` still has to infer meaning from slightly different row shapes rather than reading one strongly-defined contract
- the current viewer is useful for inspection, but still thin as a parity diff/report layer
- live lifecycle rows still need more explicit semantics for rejected / canceled / stale / partial-fill transitions

## Reference Spec

The concrete code-facing contract for this next pass lives in:
- `docs/EXECUTION_AUDIT_ROW_SCHEMA_SPEC.md`

That file should be treated as the source of truth for writer/reader field expectations during schema cleanup.

## Main Schema Gap

The biggest remaining parity problem is not price revalidation. It is row-contract drift.

Today the normalization layer can recover a useful view, but too many fields are still:
- optional without clear meaning
- adapter-specific aliases of each other
- present only in some failure paths
- derived at report time instead of guaranteed at write time

That makes it harder to answer:
- what was requested vs approved vs actually placed vs filled?
- which decision was original and which was execution-time revalidated?
- did a row fail because logic rejected it, because execution drift rejected it, or because order placement/lifecycle failed later?
- is a missing field actually missing, or just named differently in another adapter?

## Required Next Pass

### Step 1: Define a canonical execution/audit row contract
Update docs and code paths around:
- `bot/paper_adapters.py`
- `bot/live_execution.py`
- `bot/trade_audit.py`
- `bot/parity_audit.py`

At minimum, the shared row contract should explicitly define:
- identity fields: `timestamp`, `trade_id`, `market_id`, `event_key`, `direction`, `exchange`
- lifecycle fields: `status`, `failure_stage`, `lifecycle_state`
- sizing fields: `requested_size`, `approved_size`, `placed_size`, `filled_size`, `remaining_size`, `reserved_capital`
- pricing fields: `market_price`, `entry_price`, `fill_price`, `estimated_fill_price`, `slippage_estimate`
- reasoning fields: `decision_reason`, `decision_reason_code`, `original_decision_reason_code`, `execution_decision_reason_code`
- parity fields: `parity_mode_enabled`, `execution_revalidated`, `execution_revalidation_outcome`, `execution_snapshot_source`
- snapshot payloads: `original_signal_snapshot`, `execution_snapshot`
- account-state context fields where practical: `available_cash_before`, `available_cash_after_entry`

Also add explicit invariants for impossible combinations, for example:
- `filled_size <= placed_size <= approved_size <= requested_size`
- rejected rows should not masquerade as filled rows
- `execution_revalidated=false` should not carry execution-only decision fields unless clearly marked historical/unknown
- `execution_snapshot_source` should use a small fixed enum like `book|fallback|missing|unknown`

### Step 2: Harden the parity diff/report layer that already exists
Update:
- `bot/parity_audit.py`
- `scripts/parity_viewer.py`
- tests for report normalization/diffing

Goal:
- stop treating the parity report as a thin viewer only
- turn it into a first-class schema/delta surface

The report layer should clearly surface:
- rows missing required contract fields
- rows with contradictory sizing/lifecycle states
- parity revalidation deltas (original vs execution reason codes, original vs execution price inputs)
- snapshot-source breakdowns (`book`, `fallback`, `missing`)
- lifecycle outcome breakdowns (`rejected`, `placed`, `partial`, `filled`, `canceled`, `stale`, `resolved`, etc.)
- resolved outcome counts (`YES` / `NO`)
- invalid-contract row counts and top issue breakdowns
- paper/live row-shape mismatches for equivalent scenarios

### Step 3: Then harden live lifecycle + settlement behavior
Only after the row contract/reporting is cleaner, push the next hardening pass into:
- `bot/live_execution.py`
- live adapters/sync/reconciliation paths
- settlement / resolved-row mutation paths in `bot/runner.py`

Focus on:
- stale order handling
- canceled order semantics
- rejected order semantics
- partial-fill updates
- reconciliation sanity checks against persisted rows and risk state
- resolved/settlement rows using canonical `status`, `lifecycle_state`, and `outcome` semantics
- resolution events using market-outcome truth (`YES` / `NO`) instead of bot-relative win/loss labels

This ordering matters because lifecycle work is much easier to verify once the row/report layer can show exactly what changed.

## Suggested Acceptance Criteria for the Next Pass

1. Paper and live both emit a documented shared execution/audit row contract for new rows
2. The parity normalization layer no longer needs to guess between multiple aliases for core required fields
3. The parity viewer/report can explicitly show schema gaps and behavior deltas, not just raw rows
4. Tests cover row invariants and parity diff summaries in addition to decision parity
5. Lifecycle hardening can build on the same contract without redefining row semantics again

## Non-Goals For This Pass

Do not expand this pass into:
- partial-fill simulation in paper
- full settlement/accounting unification
- exchange microstructure realism overhaul
- broad UI/dashboard redesign

Those can follow once the shared row contract and parity report are solid.

## Bottom Line

The repo has already crossed the line from “parity mode not built” to “parity mode exists, but its observability contract needs tightening.”

So the best next implementation pass is:
- unify the shared execution/audit row schema
- harden the parity diff/report layer that already exists
- then use that clearer surface to harden live order lifecycle behavior
