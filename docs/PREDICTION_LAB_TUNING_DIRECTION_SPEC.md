# Prediction Lab Tuning Direction Spec

_Last updated: 2026-04-29_

## Purpose

This doc explains the current direction after the parity and live-lifecycle hardening passes:

- we are in a relatively strong state for a **tiny supervised live canary**
- we are **not** ready to scale unattended live yet
- the highest-leverage near-term work is to use **Prediction Lab and adjacent review tools** to improve how the bot finds and filters trades

This is the bridge between:
- `docs/LIVE_CANARY_READINESS.md`
- `docs/LIVE_DEPLOYMENT_READINESS_PLAN.md`
- `docs/architecture/prediction_lab_long_run_collection_spec.md`

---

## Core thesis

Prediction Lab should now be treated as a **logic-calibration and opportunity-discovery engine**, not just a collector.

The current project direction is:

1. keep live deployment framing conservative
2. use Prediction Lab to observe a much broader decision surface than the capital-constrained live bot can safely touch
3. tighten trade-finding logic based on repeated evidence
4. carry only the strongest improvements back into supervised live

In short:

> **use Prediction Lab to make the mind better before giving the hands more freedom**

---

## What this means in practice

### Prediction Lab's immediate job

Prediction Lab should help answer:
- which markets are we systematically under-trading?
- which trades look good in theory but decay under parity/live-style revalidation?
- which confidence buckets are overconfident or underconfident?
- which market groups or city/source combinations are genuinely strong?
- which cheap hidden-gem opportunities are signal vs illusion?
- which rejection rules are protecting us vs just being noisy?

### Live's immediate job

Live should remain:
- tiny
- supervised
- conservative
- used to validate operational correctness and exchange-truth alignment

Live is **not** the main place to discover new strategy behavior right now.
Prediction Lab is.

---

## Near-term workstreams

## Workstream A — Prediction Lab as research surface

### Goal
Make Prediction Lab the primary place to inspect broad market behavior and candidate strategy changes.

### Build/use priorities
- run broad periodic collection without hammering the API
- preserve snapshots and decision reasons at scale
- make repeated-opportunity analysis easy
- compare original signal decisions vs execution-time/parity-style revalidation outcomes
- summarize where promising trades came from and where they failed

### Questions to answer
- what classes of trades most often clear thresholds?
- what classes almost clear but fail for one recurring reason?
- where do hidden-gem rules help?
- where do hidden-gem rules over-admit noise?
- are there better ranking signals than the current edge/confidence ordering?

---

## Workstream B — Logic tuning loop

### Goal
Turn collected Prediction Lab evidence into disciplined strategy changes.

### Loop
1. collect broad market decisions and snapshots
2. review reports by group / confidence bucket / rejection reason / outcome
3. identify one narrow hypothesis
4. implement a small rule or ranking adjustment
5. rerun Prediction Lab / parity checks
6. only promote changes that improve evidence, not just intuition

### Good candidate tuning targets
- hidden-gem gating and ranking
- confidence calibration by market family
- weather-specific bucket handling
- event overlap / retrade controls
- market-group allow/deny filters
- score thresholds and ranking tie-breakers
- “almost good” trades rejected by one recurring condition

### Bad tuning pattern to avoid
- stacking many heuristics at once
- changing thresholds without before/after evidence
- using live P&L anecdotes alone as proof
- mixing strategy changes with lifecycle hardening changes in the same pass

---

## Workstream C — Promotion path into supervised live

### Goal
Only move logic improvements into live after they survive paper/parity review.

### Promotion criteria
A strategy change is ready for supervised live only if:
- the hypothesis is explicit
- Prediction Lab evidence improves in a legible way
- parity/live-style revalidation does not expose obvious drift
- reason-code patterns become clearer rather than muddier
- risk/exposure implications are understood
- the change is isolated enough to revert cleanly

### Suggested live promotion shape
- keep canary bankroll/risk caps tiny
- ship one narrow logic improvement at a time
- inspect first live examples manually
- compare exchange truth, local audit rows, and Prediction Lab expectations

---

## Remaining live-readiness items that still matter

Prediction Lab tuning does **not** replace the remaining live hardening work.
The following still matter before unattended deployment:

- partial / canceled / rejected / stale live order handling
- broader restart/reconnect chaos coverage
- reconciliation mismatch severity and pause behavior
- richer live audit snapshots and parity diff ergonomics
- broader settlement/accounting coverage and invariants
- operational guardrails for longer-running live service behavior

These remain governed by:
- `docs/LIVE_DEPLOYMENT_READINESS_PLAN.md`
- `docs/LIVE_LIFECYCLE_HARDENING_SPEC.md`
- `docs/LIVE_IMPLEMENTATION_BUILD_ORDER_CHECKLIST.md`

---

## Immediate recommended sequence

### Phase 1 — research first
- keep docs aligned with the current branch state
- run/inspect Prediction Lab as the broad discovery surface
- identify the most promising trade-selection weaknesses

### Phase 2 — narrow tuning passes
- implement one small strategy/ranking improvement at a time
- verify with Prediction Lab outputs and parity-style checks
- preserve clear before/after evidence

### Phase 3 — supervised live confirmation
- promote only the strongest improvements into tiny supervised live
- keep operational risk caps strict
- validate that exchange-truth behavior still looks boring and correct

### Phase 4 — return to heavier live-autonomy hardening
- once trade-finding logic is stronger and better calibrated, continue closing the remaining unattended-live safety gaps

---

## Definition of success for this direction

This direction is working if:
- Prediction Lab becomes the main place we learn about trade quality
- strategy changes are smaller, clearer, and more evidence-backed
- live canary behavior stays conservative while logic improves
- the project stops conflating “safer live runtime” with “better trade selection”

---

## Bottom line

The bot is in a good enough state that the next high-value move is **not** “immediately scale live.”

The next high-value move is:

> **use Prediction Lab and related review surfaces to improve how we discover and rank trades, then feed only the best proven changes into supervised live.**
