# Live Deployment Readiness Plan

_Last updated: 2026-04-28_

## Purpose

This doc is the bridge between:
- `docs/LIVE_CANARY_READINESS.md` (tiny supervised first live)
- `docs/LIVE_LIFECYCLE_HARDENING_SPEC.md` (next hardening branch)

Use it when the question is not:
- "can we try a tiny supervised live canary?"

but instead:
- "what must become true before unattended or true deployment is responsible?"

---

## Core framing

True deployment needs both:

### 1. Heart — operational safety under stress
The bot must fail conservatively when reality gets messy.

### 2. Mind — trustworthy self-understanding
The bot must be able to explain what it believed, what changed, what it did, and whether exchange truth agrees.

If either is weak, unattended deployment is premature.

---

## Deployment standard

The target is **not** merely:
- shared decision parity
- first live order placement
- a small supervised canary that mostly works

The target is:
- conservative behavior under ambiguous exchange/API conditions
- restart-safe handling of unresolved orders and positions
- explainable trade/accounting state after fills, cancels, stale orders, and settlement
- enough reporting and invariants that unsafe drift becomes obvious quickly
- sustained supervised evidence before autonomy is increased

---

## Workstream A — Heart: live operational hardening

### A1. Reconciliation must become first-class
Before unattended deployment, reconciliation should be able to reliably reconstruct:
- open positions
- resting orders
- filled exposure
- pending exposure
- reserved capital
- available cash
- lifecycle corrections required by exchange truth

Required:
- define and enforce a clearer reconciliation contract
- classify mismatch severity (log-only vs rewrite-and-continue vs pause)
- persist useful reconciliation snapshots/events
- ensure exchange truth wins over local optimism when available

### A2. Failure handling must become conservative
Required:
- safe retry policy for non-terminal exchange/API failures
- explicit degraded-mode behavior for stale books / delayed exchange responses / uncertain order confirmation
- stronger pause behavior when reconciliation remains contradictory
- explicit handling for duplicate-intent and idempotency-style conflicts
- clear rules for when the bot must stop trying rather than keep poking the exchange

### A3. Lifecycle correctness must hold across messy transitions
Required live coverage and semantics for:
- rejected orders
- failed placements
- placed-but-unfilled orders
- partial fills that later fill
- partial fills that later cancel
- stale/expired resting orders
- settlement/resolution transitions

Success means rows and in-memory state remain consistent for:
- `requested_size`
- `approved_size`
- `placed_size`
- `filled_size`
- `remaining_size`
- `reserved_capital`
- canonical `status`
- canonical `lifecycle_state`

### A4. Runtime invariants must be broad and loud
Before true deployment, invariant coverage should include at least:
- reserved capital never negative
- available cash never negative
- open order exposure + open position exposure reconcile to risk/account state
- a resting order is never treated as a filled position
- a placed-but-unfilled order is never written as `filled`
- a canceled partial retains filled exposure history correctly

Invariant failures should be able to:
- annotate rows
- emit alerts/logs
- trigger safety pause when severity warrants

---

## Workstream B — Mind: observability and explainability

### B1. Parity reporting must graduate from debug surface to operator surface
The existing parity/report layer is useful, but still too thin for deployment confidence.

Required:
- clearer schema-gap surfacing
- clearer lifecycle contradiction surfacing
- clearer original-vs-revalidated decision delta summaries
- clearer snapshot-source breakdowns (`book`, `fallback`, `missing`, `unknown`)
- exportable comparison artifacts for longer runs
- stronger joins/summaries so humans do not need to inspect raw rows manually

### B2. Direct-comparison lane for identical-risk runs
Before trusting live vs paper differences, the operator should be able to answer:
- was this difference caused by risk presets?
- by execution drift?
- by lifecycle/reconciliation behavior?

Required:
- explicit docs/config for intentional live-vs-paper risk differences
- a clean identical-risk comparison lane
- fixtures proving equivalent decisions under intentionally matched inputs

### B3. Persist richer before/after account-state context
Required:
- before/after account state around placement attempts
- before/after account state around reconciliation corrections
- richer persisted live execution snapshots
- cleaner reason-code normalization for reject/fail/cancel/resolve paths

This is what lets us answer "what changed and why?" after something ugly happens.

---

## Workstream C — Proof: tests needed before autonomy expands

### Minimum additional test priorities
1. stale / rejected / canceled / partial lifecycle coverage in live paths
2. restart/reconnect chaos-style tests during unresolved orders
3. richer reconciliation mismatch tests
4. settlement/accounting tests for YES/NO outcomes, fees, unsettled states, and edge transitions
5. golden or fixture-based paper/live row comparisons for equivalent scenarios
6. report/diff behavior tests, not just decision-parity tests

### Evidence standard
Do not graduate to unattended deployment because the code "looks cleaner."
Graduate because:
- targeted tests pass
- restart/reconnect tests pass
- reconciliation stays sane under edge cases
- live audit output is interpretable without guesswork
- a sustained supervised run stays boring in the best way

---

## Suggested rollout ladder

### Phase 0 — Foundation checkpoint
- branch is clean and intentional
- parity state documented accurately
- live hardening scope documented accurately

### Phase 1 — Supervised tiny canary
- tiny bankroll
- tiny position sizes
- manual review of first orders
- exchange account inspected directly after orders
- paper running in parallel

Success condition:
- no surprising lifecycle/accounting contradictions on first real trades

### Phase 2 — Supervised repeatability
- multiple live trades across multiple sessions
- restart behavior exercised safely
- reconciliation behavior reviewed after non-trivial order history
- reporting artifacts reviewed after runs

Success condition:
- lifecycle rows, account state, and exchange truth stay aligned repeatedly

### Phase 3 — Supervised limited autonomy
- still small risk caps
- bot allowed to continue longer without constant manual babysitting
- safety-pause, retry, and reconciliation behavior validated in real conditions

Success condition:
- bot fails conservatively, loudly, and explainably when conditions are degraded

### Phase 4 — True deployment readiness
Only consider this state when:
- lifecycle handling is broadly hardened
- reconciliation invariants are trusted
- settlement/accounting shape is explainable
- reporting surfaces are strong enough for postmortems
- supervised evidence is sustained, not anecdotal

---

## Definition of ready for true deployment

The bot is ready for true deployment when all of the following are true:

- it can survive restarts with unresolved orders/positions conservatively
- it does not silently drift away from exchange truth
- it does not keep retrying through ambiguous exchange state
- it can explain every live row in canonical lifecycle/accounting terms
- parity/live comparisons can distinguish logic drift from risk drift from execution/reconciliation drift
- operators can detect unsafe state quickly from artifacts/logs without reconstructing events by hand
- supervised live evidence stays stable long enough to justify more trust

---

## What to do next

Recommended next implementation sequence:
1. harden parity diff/report ergonomics
2. harden live lifecycle + reconciliation behavior
3. add broader runtime invariants and safety-pause coverage
4. add restart/reconnect chaos coverage
5. improve settlement/accounting normalization
6. rerun supervised canary under strict caps
7. only then discuss unattended deployment

---

## Bottom line

A tiny supervised canary is about proving the bot can touch reality.

True deployment is about proving the bot can:
- survive reality
- understand reality
- and stop safely when reality is unclear

That is the standard from here.