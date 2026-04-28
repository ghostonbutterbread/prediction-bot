# Live Parity Implementation Brief

## Objective

Reflect the repo's current parity state accurately, then define the next concrete implementation pass.

Parity mode itself is no longer the main gap.

The repo has now moved into a new phase:
1. the shared execution/audit contract is substantially in place
2. parity-mode observability exists and is already useful
3. the main remaining work is live operational hardening, with parity follow-up focused on reporting ergonomics and direct-comparison tooling

## Status Update

The execution/audit contract pass is now substantially in place, and the checklist has been updated to reflect that.

This brief should now be read mainly as:
- the record of what parity groundwork is already done
- the list of parity-adjacent follow-ups that still matter
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
- Shared execution snapshot normalization exists and is already used in live and paper-facing paths
- Paper parity mode records original vs execution-time snapshot metadata
- Paper and live both flow through a documented execution/audit contract in `bot/trade_audit.py`
- `bot/parity_audit.py` can normalize, validate, and summarize paper/live rows through one comparable surface
- `scripts/parity_viewer.py` provides a local inspection layer over normalized parity data
- Account-state parity, hidden-gem parity, retrade parity, recovery parity, and several lifecycle edge cases already have direct tests
- Live lifecycle hardening has already advanced meaningfully: identity gating, duplicate-intent blocking, uncertain-placement blocking, reconciliation refreshes, partial-fill handling, canonical lifecycle states, and repeated-failure safety pause behavior are in place

What is still weak:
- parity reporting is still more of an inspection/debug surface than a polished diff/report product
- direct-comparison ergonomics are still thinner than the raw parity data now available
- live lifecycle behavior is much stronger, but still not yet fully production-hardened under the messiest exchange/API edge cases
- config/documentation for intentionally different live-vs-paper risk presets is still incomplete

## Reference Spec

The concrete code-facing contract for this next pass lives in:
- `docs/EXECUTION_AUDIT_ROW_SCHEMA_SPEC.md`

That file should be treated as the source of truth for writer/reader field expectations during schema cleanup.

## Main Remaining Gap

The biggest remaining parity problem is no longer basic price revalidation, and it is no longer the absence of a row contract.

The main parity-adjacent gaps now are:
- report ergonomics and clearer parity delta surfacing
- a cleaner direct-comparison workflow when paper and live should run under intentionally identical risk assumptions
- broader live operational correctness around messy order lifecycle and reconciliation conditions

The important shift is:
- **before**: parity needed core infrastructure
- **now**: parity has the core infrastructure, and the remaining work is mostly visibility, comparison ergonomics, and live safety

## Required Next Pass

### Step 1: Finish hardening the parity diff/report layer that already exists
Update:
- `bot/parity_audit.py`
- `scripts/parity_viewer.py`
- report-oriented tests and fixtures

Goal:
- stop treating the parity report as a thin viewer only
- turn it into a first-class schema/delta surface
- make it easier to answer "what drifted and why?" without reading raw rows by hand

The report layer should clearly surface:
- rows missing required contract fields
- rows with contradictory sizing/lifecycle states
- parity revalidation deltas (original vs execution reason codes, original vs execution price inputs)
- snapshot-source breakdowns (`book`, `fallback`, `missing`)
- lifecycle outcome breakdowns (`rejected`, `placed`, `partial`, `filled`, `canceled`, `stale`, `resolved`, etc.)
- resolved outcome counts (`YES` / `NO`)
- invalid-contract row counts and top issue breakdowns
- paper/live row-shape mismatches for equivalent scenarios
- better summaries/exportable artifacts for longer parity runs

### Step 2: Add a clean direct-comparison lane for identical-risk parity runs
Update:
- config/docs for live-vs-paper risk differences
- parity-mode configuration surface
- fixture coverage around intentionally matched inputs

Goal:
- make it explicit when paper/live are intentionally using different presets
- add a straightforward way to run parity comparisons under identical risk assumptions when that is the question

This is less about core parity correctness and more about operator clarity.

### Step 3: Continue live lifecycle + settlement hardening
Push the next hardening pass into:
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
- broader transient exchange/API inconsistency handling

This is now the main production-readiness lane.

## Suggested Acceptance Criteria for the Next Pass

1. The parity viewer/report can explicitly show schema gaps and behavior deltas, not just raw rows
2. Longer parity runs can produce clearer summaries or exportable artifacts for comparison
3. Live-vs-paper risk differences are explicit in config/docs, and there is a clean identical-risk comparison lane
4. Tests cover report/diff behavior in addition to decision parity and row invariants
5. Live lifecycle hardening continues on top of the existing execution/audit contract without redefining row semantics again

## Non-Goals For This Pass

Do not expand this pass into:
- partial-fill simulation in paper
- full settlement/accounting unification
- exchange microstructure realism overhaul
- broad UI/dashboard redesign

Those can follow once the shared row contract and parity report are solid.

## Bottom Line

The repo has already crossed the line from “parity mode not built” to “parity mode is real, strongly grounded, and no longer the main blocker.”

So the best next implementation pass is:
- harden the parity diff/report layer that already exists
- add a cleaner direct-comparison lane for identical-risk parity runs
- keep pushing live order/reconciliation/settlement hardening as the main operational-readiness track
