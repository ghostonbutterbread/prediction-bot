# Prediction Bot Docs Index

_Last updated: 2026-04-26_

## Why this file exists

This file is the fast orientation map for the current docs set.

Use it when you need to answer:
- which spec should I read first?
- which doc is strategic vs implementation-level?
- which docs describe the current branch vs the next branch?
- what is the planned direction of the project from here?

This index reflects the current direction:
- the current `feature/prediction-lab` branch should be cleaned, committed, and merged
- Prediction Lab is considered good enough if it supports long-run scaled decision capture
- parity is materially improved but not fully finished
- the next branch should focus on live lifecycle hardening
- deferred parity follow-ups remain tracked and intentional

---

## Recommended reading order

### 1. `docs/OVERALL_DIRECTION_SPEC.md`
Start here for the big picture.

Use it to understand:
- what this branch is trying to achieve
- why Prediction Lab and parity are related but distinct
- what is good enough to checkpoint now
- why live hardening is the next branch
- what parity work is intentionally deferred, not abandoned

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
Read this when starting the next branch.

Use it to understand:
- why live correctness is now the main blocker
- what work belongs in the next branch
- how to harden reconciliation, lifecycle transitions, startup safety, settlement, and failure handling

This is the main spec for the upcoming live-hardening workstream.

---

### 7. `docs/LIVE_CANARY_READINESS.md`
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
- `LIVE_PARITY_CHECKLIST.md`
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
- `docs/architecture/shared_core_adapters_design.md` — shared-core/adapters split
- `docs/architecture/event_retrade_spec.md` — event-aware retrade behavior
- `docs/architecture/event_retrade_v2_spec.md` — stronger retrade follow-up pass
- `docs/architecture/paper_live_rollout_log.md` — rollout logging concept
- `docs/architecture/paper_live_rollout_log_and_standby_spec.md` — rollout + standby design
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
- not overloading the market/API

Success for now means it can:
- run repeatedly or indefinitely
- scan on controlled intervals like 15 minutes
- record decisions and snapshots at scale
- help us inspect what the bot would do across the market

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
- next priority after this branch is merged

---

## Simple rule for future changes

### Put work on the current/merge branch if it mostly improves:
- Prediction Lab long-run usefulness
- branch cleanup and merge readiness
- parity visibility that is needed to explain current behavior clearly

### Put work on the next branch if it mostly improves:
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

That reading path matches the current plan:
- merge this branch
- then do live hardening
- then return for deferred parity cleanup
