# Prediction Bot Docs Index

_Last updated: 2026-05-13_

## Why this file exists

This file is the fast orientation map for the current docs set.

Use it when you need to answer:
- which spec should I read first?
- which doc is strategic vs implementation-level?
- which docs describe the current branch vs the next branch?
- what is the planned direction of the project from here?

This index reflects the current direction:
- Prediction Lab is now a primary trade-finding and calibration surface
- parity and live-lifecycle groundwork are materially improved but not fully finished
- tiny supervised live is plausible with strict caps
- unattended live still needs more lifecycle/reconciliation/audit hardening
- the immediate high-value direction is to use Prediction Lab evidence to improve trade selection

---

## Recommended reading order

### 1. `docs/OVERALL_DIRECTION_SPEC.md`
Start here for the big picture.

Use it to understand:
- where the project is now
- why Prediction Lab, parity, and live hardening are related but distinct
- why Prediction Lab is now the main trade-finding surface
- why tiny supervised live is plausible but unattended live is still premature
- what work remains intentionally active

If you only read one doc first, read this one.

---

### 2. `LIVE_PARITY_CHECKLIST.md`
Read this next for the status board.

Use it to understand:
- what parts of parity are implemented
- what is partial
- what is still missing
- why parity is strong enough to checkpoint but not fully complete

This is the quickest operational snapshot of parity status.

---

### 3. `docs/LIVE_PARITY_MODE_SPEC.md`
Read this for parity design intent.

Use it to understand:
- what parity mode is supposed to do
- what it is explicitly not supposed to do
- how parity mode fits between normal paper mode and real live trading
- the architecture rule that shared decision inputs should be normalized once, not duplicated across paper/live

This doc defines the conceptual role of parity mode.

---

### 4. `docs/LIVE_PARITY_IMPLEMENTATION_BRIEF.md`
Read this for the transition from "parity exists" to "parity/reporting quality needs cleanup."

Use it to understand:
- why row-contract cleanup matters
- why parity reporting is not just a nice UI but part of verification
- why the next quality pass must tighten the execution/audit contract

This is the bridge between parity design and implementation quality.

---

### 5. `docs/EXECUTION_AUDIT_ROW_SCHEMA_SPEC.md`
Read this for concrete row semantics.

Use it when working on:
- shared execution/audit row fields
- writer/reader contracts
- invariants around requested/approved/placed/filled/remaining sizing
- paper/live row compatibility

This is the low-level contract doc.

---

### 6. `docs/LIVE_LIFECYCLE_HARDENING_SPEC.md`
Read this when working on the remaining live-safety gap.

Use it to understand:
- why live correctness is still the blocker for unattended deployment
- how to harden reconciliation, lifecycle transitions, startup safety, settlement, and failure handling

This is the main spec for the remaining live-hardening workstream.

---

### 7. `docs/PAPER_WEATHER_FINDINGS_2026-05-04.md`
Read this for the freshest paper-weather findings.

Use it to understand:
- what the current 15 open paper trades are showing
- why some trades look golden-ish while others are cheap lottery/hidden-gem trades
- why forecast-direction side selection matters, especially buying NO when evidence rejects YES
- why an explicit market allowlist gate is now recommended

This is the quickest handoff note for future agents working on weather strategy, paper-trade quality, or market gating.

---

### 8. `docs/architecture/market_router_strategy_lane_spec.md`
Read this before implementing weather strategy lanes or market-gating fixes.

Use it to understand:
- why market routing must fail closed before paper/live trade entry
- why current weather trading should require strict daily-temperature route evidence
- how to prevent non-weather markets like 2030 energy/WIND from leaking into weather runs
- how future category handlers can share root EV/risk/execution logic without data leaks

This is the immediate pre-lane bugfix/spec for market routing and category handlers.

---

### 9. `docs/architecture/strategy_lane_shadow_validation_spec.md`
Read this when working on beta-shadow lane validation and old-vs-new evidence.

Use it to understand:
- why beta-shadow configs must enable real lane/cap settings
- why paper must record stable-skip shadow-intent candidates
- what remains blocked before exact apples-to-apples lane PnL is trustworthy

This is the current spec for making lane shadow evidence useful without changing paper/live execution.

---

### 10. `docs/PREDICTION_LAB_TUNING_DIRECTION_SPEC.md`
Read this for the current immediate strategy direction.

Use it to understand:
- why Prediction Lab should now drive trade-finding improvements
- how to use broad paper evidence without overtrusting live anecdotes
- how to promote only strong logic changes into supervised live

This is the strategy-iteration bridge between current readiness and future deployment.

---

### 10. `docs/LIVE_DEPLOYMENT_READINESS_PLAN.md`
Read this when the question becomes "what must be true before unattended or true deployment is responsible?"

Use it to understand:
- the gap between supervised canary and true deployment
- the required operational-safety (heart) work
- the required observability/explainability (mind) work
- the rollout ladder from canary to limited autonomy to true deployment

This is the main bridge from current hardening work to real deployment readiness.

---

### 10. `docs/LIVE_IMPLEMENTATION_BUILD_ORDER_CHECKLIST.md`
Read this when you want the concrete implementation order.

Use it to understand:
- what to build first vs later
- which phases belong on the next live-hardening branch
- which files/tests each phase should probably touch
- what exit criteria should gate movement to the next phase

This is the execution checklist that turns the broader specs into a practical build sequence.

---

### 11. `docs/LIVE_CANARY_READINESS.md`
Read this only when preparing a tiny supervised live test.

Use it to understand:
- what is safe enough for a first canary
- what is still not safe for unattended live
- what limits/checks should be in place before the first small real-money run

This is a launch-readiness note, not a build spec.

---

## How the docs fit together

### Strategic layer
These tell us where the project is going:
- `docs/OVERALL_DIRECTION_SPEC.md`
- `docs/PAPER_WEATHER_FINDINGS_2026-05-04.md`
- `docs/PREDICTION_LAB_TUNING_DIRECTION_SPEC.md`
- `LIVE_PARITY_CHECKLIST.md`
- `docs/LIVE_DEPLOYMENT_READINESS_PLAN.md`
- `docs/LIVE_IMPLEMENTATION_BUILD_ORDER_CHECKLIST.md`
- `docs/LIVE_CANARY_READINESS.md`

### Design/spec layer
These explain intended behavior and boundaries:
- `docs/LIVE_PARITY_MODE_SPEC.md`
- `docs/LIVE_LIFECYCLE_HARDENING_SPEC.md`

### Contract/implementation layer
These define what code and tests should enforce:
- `docs/LIVE_PARITY_IMPLEMENTATION_BRIEF.md`
- `docs/EXECUTION_AUDIT_ROW_SCHEMA_SPEC.md`

### Supporting architecture lane
These are narrower supporting specs for adjacent workstreams:
- `docs/BETA_SHADOW_WEATHER_RUNBOOK.md` — safe beta-shadow weather paper/Prediction Lab runtime commands and data-root separation
- `docs/PAPER_DUAL_WALLET_MIGRATION_CANARY.md` — read-only Phase 5 migration/canary plan for preserving existing stable/beta paper accounting while previewing later shared-candidate cutover
- `docs/architecture/shared_core_adapters_design.md` — shared-core/adapters split
- `docs/architecture/event_retrade_spec.md` — event-aware retrade behavior
- `docs/architecture/event_retrade_v2_spec.md` — stronger retrade follow-up pass
- `docs/architecture/paper_live_rollout_log.md` — rollout logging concept
- `docs/architecture/paper_live_rollout_log_and_standby_spec.md` — rollout + standby design
- `docs/architecture/prediction_lab_shared_pipeline_spec.md` — shared collector/replay/paper/live decision-pipeline architecture for Prediction Lab as a wrapper, not duplicated logic
- `docs/architecture/prediction_lab_shadow_delta_spec.md` — beta/shadow comparison metadata as row-level Prediction Lab deltas, not duplicate trade or prediction streams
- `docs/architecture/prediction_lab_blind_replay_validator_spec.md` — blind replay, separate resolution ledger, replay-output table, and active dataset validator requirements
- `docs/architecture/prediction_lab_backfill_upgrader_spec.md` — old-row inventory, artifact/log/historical recovery, evidence tiers, and provenance-labeled upgraded datasets
- `docs/architecture/prediction_lab_long_run_collection_spec.md` — Prediction Lab long-run collection direction
- `docs/architecture/prediction_lab_long_run_collection_v1_spec.md` — earlier/v1 collection version

---

## Current project direction in one glance

### Prediction Lab
Primary role:
- long-term scale
- periodic market scanning
- broad decision capture
- training/review visibility
- trade-finding calibration
- not overloading the market/API

Success for now means it can:
- run repeatedly or indefinitely
- scan on controlled intervals like 15 minutes
- record decisions and snapshots at scale
- help us inspect what the bot would do across the market
- highlight where the logic should be tightened, relaxed, or re-ranked

### Parity
Primary role:
- make paper more execution-realistic before live
- show original-vs-revalidated decision drift
- improve confidence that paper results mean something pre-live

Current state:
- strong enough to checkpoint
- not fully finished

### Live hardening
Primary role:
- make live behavior safe under real-world messiness
- protect against reconciliation drift, uncertain orders, restart ambiguity, and lifecycle mistakes

Current state:
- still required for unattended live
- should continue, but does not replace the immediate need to improve trade selection via Prediction Lab

---

## Simple rule for future changes

### Prioritize work now if it mostly improves:
- Prediction Lab long-run usefulness
- trade-finding analysis and calibration
- parity visibility that is needed to explain current behavior clearly

### Treat work as deployment-hardening lane if it mostly improves:
- live lifecycle correctness
- restart/recovery safety
- reconciliation severity behavior
- duplicate-submission protection
- unattended-live safety

---

## Bottom line

If you are unsure where to start:
1. read `docs/OVERALL_DIRECTION_SPEC.md`
2. check `LIVE_PARITY_CHECKLIST.md`
3. use `docs/LIVE_LIFECYCLE_HARDENING_SPEC.md` for the next branch
4. use `docs/LIVE_DEPLOYMENT_READINESS_PLAN.md` for the full road from canary to true deployment
5. use `docs/LIVE_IMPLEMENTATION_BUILD_ORDER_CHECKLIST.md` for the concrete build order

That reading path matches the current plan:
- use Prediction Lab to improve trade-finding logic deliberately
- keep tiny supervised live conservative
- continue the remaining live-hardening work needed for unattended deployment
- use the deployment-readiness plan to sequence rollout and proof
- use the build-order checklist when doing heavier lifecycle/reconciliation implementation
