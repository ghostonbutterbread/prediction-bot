# Live Lifecycle Hardening Spec

_Last updated: 2026-04-26_

## Goal

Take the bot from:
- **parity-capable and canary-capable**

to:
- **operationally trustworthy for unattended live trading**

This spec is the next pass after the execution/audit contract cleanup.

The core idea is simple:
- decision parity is no longer the main blocker
- live operational correctness is now the main blocker
- the next work should harden the real live order/position lifecycle around exchange truth, reconciliation, settlement, and failure handling

## Sequencing Note

This live-hardening pass should be treated as the next branch/workstream after the current parity / Prediction Lab branch is brought to a clean, committable checkpoint.

It does **not** mean parity is "finished forever." It means parity is strong enough to checkpoint now, then resume later for the remaining follow-ups once live lifecycle safety is in a better place.

When this spec is complete enough to merge, explicitly return to the deferred parity items:
- canonical execution/audit row enforcement
- richer parity diff/report ergonomics
- broader restart/recovery parity proofs

---

## What Just Became Stronger

The repo now has a much better foundation than before:

- paper and live are closer on execution/audit row shape
- parity/audit reporting can surface schema gaps and lifecycle contradictions more clearly
- live rows now better distinguish requested / approved / placed / filled / remaining semantics
- parity traces now better preserve original vs execution-time decision context

That means the next pass should stop focusing on whether parity mode exists and start focusing on whether **live behavior stays correct under messy real conditions**.

---

## Problem Statement

The live adapter now knows how to:
- revalidate at execution time
- write canonical-ish rows
- distinguish some placed / partial / filled / rejected states

But production live trading still depends on stronger handling for:
- resting orders that remain open for a while
- partially filled orders that later fill, cancel, or stale out
- exchange/API disagreement with local assumptions
- restart/reconnect with unresolved orders and positions
- settlement transitions after markets resolve
- repeated transient failures that should trigger safe pauses instead of repeated order attempts
- rate limits, delayed exchange responses, and uncertain order-confirmation windows
- operator/account mistakes such as pointing live mode at the wrong account, credential set, or environment
- manual exchange-side actions that the bot did not initiate but must reconcile cleanly

This is the real gap between:
- **tiny supervised canary**
and
- **unattended production live**

---

## Design Principles

### 1. Exchange truth beats local optimism
If the exchange/API can tell us the order state, fill state, position state, balance, or market outcome, prefer that over local inference.

### 2. One lifecycle, many transitions
The bot should model one canonical lifecycle surface instead of inventing separate semantics per failure path.

### 3. Reconciliation is part of execution, not a side task
Live correctness depends on refresh/reconcile loops being first-class, not optional cleanup.

### 4. Restart safety is mandatory
If the bot restarts with open orders or positions, behavior must remain conservative and explainable.

### 5. Safety before throughput
When state is contradictory or suspicious, prefer:
- pause
- alert
- reconcile

over continued trading.

---

## Scope

### In scope
1. live order lifecycle hardening
2. reconciliation state contract
3. restart/recovery handling for unresolved live state
4. settlement/resolution state normalization
5. invariant alarms and kill-switch behavior
6. tests for the above

### Out of scope
1. full paper microstructure simulation
2. advanced portfolio optimization
3. exchange abstraction redesign
4. UI/dashboard overhaul
5. broad strategy changes unrelated to live correctness

---

## Workstream 1 — Canonical Live Lifecycle State Machine

## Objective
Make live rows and in-memory state consistently express these states:

- `rejected`
- `failed`
- `placed`
- `partial`
- `filled`
- `canceled`
- `stale`
- `resolved`

and make transitions between them explicit.

## Requirements

### Order states
A live order should move through canonical states based on exchange truth:
- submitted but unfilled -> `placed_open`
- partially filled and still open -> `partial_open`
- fully filled and now position-bearing -> `filled_open`
- canceled before fill -> `canceled_unfilled`
- canceled after partial fill -> `canceled_partial`
- expired/stale -> `stale_open_order`
- rejected before acceptance -> `placement_failed` or `revalidation_rejected` depending on stage

### Position states
A position should exist only when there is actual filled exposure.

The bot should not treat a resting order as a filled position.

### Row semantics
For every live row:
- `requested_size`
- `approved_size`
- `placed_size`
- `filled_size`
- `remaining_size`
- `reserved_capital`

must stay consistent with the canonical lifecycle state.

## Acceptance criteria
- a placed-but-unfilled order is not written as `filled`
- partial fills preserve both filled and remaining exposure
- canceled partials retain filled exposure history without masquerading as open placed size
- stale/expired orders have canonical stale lifecycle fields

---

## Workstream 2 — Reconciliation Contract

## Objective
Define exactly what reconciliation is responsible for and what data it is allowed to correct.

## Shared reconciliation contract
At reconciliation time, the bot should be able to reconstruct:
- open positions
- resting orders
- filled exposure
- pending exposure
- reserved capital
- available cash
- canonical trade history rows when exchange truth reveals a different state than local memory assumed

## Requirements

### Inputs
From exchange/API where available:
- balances
- open positions
- resting orders
- fill information or order status
- market resolution state

### Outputs
Reconciliation should produce a durable snapshot that can answer:
- what is currently open?
- what is pending?
- what changed since the last view?
- did any local row need correction?
- is there a mismatch severe enough to pause trading?

### Mismatch policy
Define severity classes:

#### Low severity
Examples:
- timestamp drift
- optional metadata absent

Action:
- log only

#### Medium severity
Examples:
- local open order missing but exchange says canceled
- local status `placed` but exchange says `partial`

Action:
- rewrite local state from exchange truth
- emit lifecycle/reconciliation event

#### High severity
Examples:
- negative effective cash after reconciliation
- local/exchange exposure divergence beyond threshold
- duplicate active order identity collision

Action:
- pause live trading
- emit high-priority alert
- require manual review or explicit recovery path

## Acceptance criteria
- reconciliation can convert local optimistic state into exchange-truth state safely
- open order / open position / reserved capital views stay consistent after reconcile
- repeated high-severity mismatches can halt live execution automatically

---

## Workstream 3 — Restart / Recovery Hardening

## Objective
Make bot restarts survivable when live state is non-empty.

## Requirements

### On startup with live mode enabled
The bot should:
1. load persisted local rows/state
2. fetch exchange truth
3. reconcile before attempting new live orders
4. classify the recovered state as safe / degraded / blocked

### Safe startup
Allowed when:
- exchange truth and local state broadly agree
- no unresolved severe mismatch exists

### Degraded startup
Allowed but cautious when:
- some rows need correction
- open orders exist
- partial fills exist
- startup must continue under reduced confidence

Suggested action:
- continue but mark status/reporting as degraded

### Blocked startup
Required when:
- reconciliation cannot establish trustworthy state
- unresolved high-severity mismatch persists
- duplicate ambiguous exposure exists

Suggested action:
- do not place new orders
- alert and wait for human intervention

## Acceptance criteria
- startup with open resting orders works deterministically
- startup with partial fills works deterministically
- startup with ambiguous unresolved state fails safe

---

## Workstream 4 — Settlement / Resolution Normalization

## Objective
Make live resolution rows as trustworthy as live entry rows.

## Requirements

### Outcome semantics
- `outcome` must mean market truth: `YES` or `NO`
- bot-relative success/failure should live in a separate field such as `resolution_result`

### Resolution transition
When a market resolves:
- open positions should transition to `status = resolved`
- `lifecycle_state = resolved_position`
- `resolved = true`
- `resolved_at` populated
- `pnl` populated when derivable
- `settlement_value` recorded when known

### Accounting expectations
Where possible reuse shared accounting helpers so paper/live do not diverge on simple YES/NO settlement math.

## Acceptance criteria
- live resolved rows use canonical resolution semantics
- settlement does not silently overwrite unresolved lifecycle state incorrectly
- resolution reporting distinguishes market outcome from bot win/loss

---

## Workstream 5 — Failure Handling, Idempotency, and Kill Switches

## Objective
Prevent bad live loops.

## Requirements

### Explicit failure categories
At minimum:
- revalidation failure
- placement failure
- exchange timeout
- duplicate submission suspicion
- reconciliation mismatch
- settlement failure
- rate-limit / backoff state
- operator/account-identity mismatch
- external/manual state mutation detected during reconcile

### Retry policy
Define safe retry behavior only for non-terminal failures.

Examples:
- timeout fetching order state -> retry allowed
- unknown order placement outcome -> reconcile before retry
- clear rejection from exchange -> do not retry blindly

### Idempotency expectations
If an order attempt might have succeeded but confirmation is uncertain, the system must reconcile before re-submitting.

### Kill-switch rules
Pause or halt live trading when:
- repeated high-severity reconciliation mismatches occur
- repeated exchange/API failures exceed threshold
- cash/exposure invariants go negative or contradictory
- duplicate submission risk cannot be ruled out
- live account identity/config does not match the expected operator-approved account
- manual/external mutations create unresolved ambiguity in open orders or exposure

### Account/environment safety checks
Before live order placement, the bot should be able to confirm:
- expected exchange/account identity
- expected environment/mode (`paper` vs `live`)
- intended credential/key path or account selector

If those checks fail, trading should block before any order attempt.

### Rate-limit / delayed-confirmation behavior
The bot should explicitly model the case where:
- order submission may have succeeded
- but confirmation is delayed or temporarily unavailable

That path should be treated as **uncertain state**, not immediate failure.
The correct follow-up is:
- reconcile
- re-fetch exchange truth
- avoid duplicate submission until uncertainty clears

## Acceptance criteria
- the bot does not repeatedly place orders into uncertainty
- suspicious state produces pause/alert behavior rather than silent continuation

---

## Required Tests

### Lifecycle tests
- placed -> partial -> filled transition
- placed -> canceled_unfilled transition
- partial -> canceled_partial transition
- placed -> stale transition
- rejected placement path preserves canonical row semantics

### Reconciliation tests
- local `placed` corrected to exchange `partial`
- local open order removed after exchange cancel
- local optimistic row corrected when exchange truth differs
- high-severity mismatch triggers pause/block behavior

### Restart tests
- restart with open resting order
- restart with partial fill
- restart with resolved position not yet reflected locally
- restart with ambiguous duplicate state -> blocked startup

### Settlement tests
- YES resolution for `BUY_YES`
- NO resolution for `BUY_NO`
- losing resolution paths
- accounting/pnl shape for resolved live rows

### Invariant tests
- reserved capital never negative
- available cash never negative after reconciliation
- open order + open position exposure matches risk/exchange view within tolerance
- account identity mismatch blocks trading before placement
- delayed confirmation path does not allow blind duplicate submission

---

## Implementation Order

1. define canonical live lifecycle transitions in code/tests
2. strengthen reconciliation contract and mismatch severity handling
3. harden restart/recovery flow
4. normalize settlement/resolution rows
5. add retry/idempotency/kill-switch logic
6. run supervised canary only after the above is minimally green

---

## Suggested Acceptance Bar For "Unattended Live Ready"

Do **not** call the bot unattended-live-ready until all of the following are true:

1. live lifecycle transitions are explicit and tested
2. restart with open orders/partial fills is safe and deterministic
3. reconciliation mismatch alarms and pause behavior exist
4. settlement rows are canonical and trustworthy
5. duplicate-submission risk is guarded by reconcile-before-retry logic
6. a tiny supervised live canary has completed without unexplained state drift

---

## Recommended Rollout

### Stage 1 — Current
- paper good
- parity useful
- tiny supervised live canary possible

### Stage 2 — After this spec
- supervised live more trustworthy
- restart/recovery safer
- lifecycle/accounting more explainable

### Stage 3 — After canary evidence
- consider unattended live with tight caps

---

## Bottom Line

The project is now at the point where **decision parity is no longer the main story**.

The next real milestone is:

> make live behavior safe under partial fills, resting orders, reconciliation drift, restart, and settlement.

That is the bridge from "promising live canary" to "production-ready live system."