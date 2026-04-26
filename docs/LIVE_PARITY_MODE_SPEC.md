# Live Parity Mode Spec

## Goal

Add an optional, config-driven **paper parity mode** that lets paper trading simulate the final live revalidation step without forcing that extra complexity into every paper experiment.

Parity mode is a paper-only feature. It is not a live trading mode, and it is not meant to replace the live adapter.

This preserves three distinct roles:

1. **Logic-first paper mode**
   - Fewer moving parts
   - Best for rapid strategy iteration
   - Tests core trading logic, policy changes, and whether we are trading the right parts of the market

2. **Parity paper mode**
   - Still paper
   - Adds a final execution-style revalidation pass
   - Better approximation of live behavior under market movement
   - Best for pre-production confidence and parity testing

3. **Live trading**
   - The real adapter
   - Interacts with the API, places orders, sits on markets when thresholds are met, and continues through the real order/position lifecycle

The feature should be **enabled or disabled via config**, not hard-coded.

---

## Problem Statement

Today, paper and live share the core decision engine (`build_trade_decision()`), but they differ in one important way:

- **Live** fetches current bid/ask before order placement, rebuilds the trade context, and reruns shared-core approval using execution-time prices.
- **Paper** mostly evaluates the original strategy snapshot and simulates from that snapshot.

This creates a parity gap:
- a trade can pass in paper based on a scanned price
- but fail in live because the execution-time ask moved enough to invalidate the edge or hidden-gem thresholds

That gap matters for production readiness, especially in:
- low-priced hidden-gem trades
- thin books
- fast-moving markets

But it is **not always desirable** to simulate this during strategy iteration, because it adds moving pieces and can obscure pure logic evaluation.

Parity mode exists to close that gap inside paper. Its purpose is to model market movement and execution-time drift before going live, while normal paper mode remains the simpler logic lab and live remains the real execution path.

---

## Design Principles

1. **Config-driven, not mandatory**
   - parity mode must be opt-in
   - default paper mode should remain simple unless explicitly enabled

2. **Preserve shared decision logic**
   - no duplicating approval logic
   - parity mode should only change the price/context source used for the final paper approval pass

3. **Promote shared decision-prep logic upward**
   - if pricing normalization, execution snapshot construction, or decision-prep behavior is needed by both paper and live, it must be extracted into a shared helper before parity mode depends on it
   - do not let paper and live hand-roll separate interpretations of the same execution-time inputs
   - environment-specific behavior should stay in adapters, but shared decision inputs must be normalized consistently across modes

4. **Auditability first**
   - when parity mode is enabled, paper must record both:
     - the original strategy snapshot
     - the revalidated execution snapshot
   - this is necessary so we can tell whether a rejection came from model logic or execution-time drift

5. **Paper remains a logic lab**
   - parity mode augments paper, it does not replace the simpler mode

6. **Reviewer-friendly structure**
   - new behavior should be isolated and testable
   - avoid scattering live-specific assumptions across the simulator

7. **API truth over local inference in live mode**
   - when live exchange/API state is available, prefer it over backend assumptions
   - local logic may decide, normalize, and report, but it should not invent post-submission outcomes that the API can confirm
   - this rule applies primarily to live mode; paper must synthesize its own state because no real exchange truth exists there

---

## Proposed Config

Add a new config block:

```yaml
parity_mode:
  enabled: false
  record_revalidation_snapshot: true
  require_book_prices: false
  fallback_to_signal_prices: true
```

### Field meanings

- `parity_mode.enabled`
  - top-level switch for paper/live parity features
  - when true, paper performs a final execution-style revalidation using latest market prices before simulating entry

- `parity_mode.record_revalidation_snapshot`
  - when true, paper stores both original signal snapshot and final revalidation snapshot in trade audit rows

- `parity_mode.require_book_prices`
  - if true, parity mode refuses the trade when execution-time book prices cannot be fetched
  - if false, parity mode may use fallback pricing behavior

- `parity_mode.fallback_to_signal_prices`
  - if execution-time price fetch fails and `require_book_prices` is false, use original signal prices as fallback

### Defaults

Recommended defaults:
- `enabled: false`
- `record_revalidation_snapshot: true`
- `require_book_prices: false`
- `fallback_to_signal_prices: true`

This keeps current paper behavior unchanged unless explicitly enabled.

### Config semantics

- If `parity_mode.enabled` is `false`, all parity-mode subfields are inert.
- If `parity_mode.require_book_prices` is `true`, fallback is disabled even if `fallback_to_signal_prices` is `true`.
- If `parity_mode.enabled` is `true`, paper must run the execution-time revalidation pass before final approval.
- Config precedence should be explicit in implementation and docs: defaults < YAML config < environment overrides < runtime/CLI overrides.

---

## Scope of Change

### In scope

1. Config plumbing for parity mode
2. Paper execution revalidation path
3. Audit trail additions for original vs final execution snapshot
4. Targeted tests for paper/live approval parity under matching prices
5. Documentation updates
6. Clear documentation that parity mode belongs to paper, while live remains the real execution adapter

### Out of scope for this phase

1. Turning parity mode into a live runtime mode
2. Partial-fill simulation in paper
3. Full live order lifecycle emulation in paper
4. Slippage modeling beyond simple execution-price refresh
5. Exchange-API behavior simulation beyond price refresh and revalidation

This phase is specifically about **decision parity at execution time**, not full market microstructure simulation or replacing live.

Across the broader system, the standing rule should be:
- **paper** may simulate because there is no exchange truth
- **live** should defer to API truth whenever the exchange can tell us what really happened

## Sequencing Note

Parity work is now strong enough that the main near-term blocker is no longer "does parity mode exist?" but "is live operationally safe?"

So the recommended sequence is:
1. checkpoint and merge the current parity / Prediction Lab groundwork once the branch is clean and reviewable
2. move next implementation energy into live lifecycle hardening
3. come back after that pass to finish the remaining parity follow-ups, especially:
   - canonical write-time execution/audit row enforcement
   - stronger parity diff/report surfacing
   - broader restart/recovery parity coverage

That keeps parity as an explicit follow-on lane rather than losing it during live work.

---

## Proposed Architecture Changes

## 1. Config Layer

### Files
- `bot/config.py`
- optional `.env` override support if desired
- docs/config example

### Change
Normalize and expose a `parity_mode` config block with the fields above.

### Acceptance criteria
- `load_config()` returns a complete `parity_mode` dict with defaults
- missing config does not break current behavior

---

## 2. Paper Execution Adapter: Optional Revalidation Pass

### Files
- `bot/paper_adapters.py`
- possibly `bot/simulator.py`

### Change
Before paper simulates execution, optionally perform a live-style revalidation pass.

This is a paper-only behavior. Live continues to act as the real adapter and source of execution-time truth.

For live mode specifically, exchange/API state should win over local inference whenever it is available. That means balances, open orders, partial fills, cancellations, resting status, and open positions should be derived from API truth rather than optimistic backend assumptions.

1. Start from the original signal
2. If parity mode is enabled:
   - fetch current bid/ask from exchange using the same execution-price semantics as live
   - derive execution-time prices (`yes_price`, `no_price`, `best_yes_ask`, `best_no_ask`, etc.)
   - rebuild `TradeContext` using the updated prices
   - rerun `build_trade_decision()`
3. If approved, simulate entry using the revalidated decision
4. If rejected, log that the trade was rejected during parity revalidation
5. If book fetch fails:
   - reject if `require_book_prices: true`
   - otherwise fall back to signal prices only if `fallback_to_signal_prices: true`

### Important detail
This should not copy logic from live execution. The goal is to **reuse the same shared decision function**, while building a paper-side execution snapshot that mirrors live input semantics.

Before paper parity mode uses live-style revalidation, any execution snapshot or pricing normalization logic shared by both environments must first be extracted into a shared helper and adopted by live as the canonical path.

### Acceptance criteria
- parity mode off → current paper behavior unchanged
- parity mode on → paper can reject a trade that only passed at stale snapshot price
- paper trade row records whether approval came from original signal or revalidated execution snapshot
- the execution price of record is explicitly defined and matches live semantics for final approval

---

## 3. Shared Helper for Execution Snapshot Construction

This helper should be part of phase 1 implementation, not deferred, to reduce drift risk between paper and live while parity mode is being added.

### Files
- new helper module, suggested: `bot/shared_core/execution_snapshot.py`
  or a similarly named small utility

### Purpose
Avoid duplicating the bid/ask normalization logic in both live and paper.

### Responsibilities
Given:
- raw signal
- current bid/ask data
- intended direction

Return:
- normalized execution-time pricing fields
- `market_price`
- `yes_price`
- `no_price`
- `best_yes_ask`
- `best_no_ask`
- `best_yes_bid`
- `best_no_bid`
- `estimated_fill_price`

### Why this matters
If paper and live each hand-roll this differently, parity mode will drift.

### Acceptance criteria
- both paper and live can rely on the same normalization helper
- live is refactored to use this helper before paper parity mode is added
- price-field semantics match across modes

---

## 4. Trade Audit Schema Expansion

### Files
- `bot/paper_adapters.py`
- `bot/simulator.py`
- possibly `bot/trade_audit.py`

### Add fields to paper trade rows
Suggested fields:

```json
{
  "parity_mode_enabled": true,
  "execution_revalidated": true,
  "execution_revalidation_outcome": "approved|rejected|fallback",
  "original_signal_snapshot": {...},
  "execution_snapshot": {...},
  "original_decision_reason_code": "approved",
  "execution_decision_reason_code": "hidden_gem_edge_below_threshold"
}
```

### Minimum required audit fields
At minimum, parity-enabled rows must store:
- original price inputs
- revalidated price inputs
- original approval status
- original decision reason code
- final approval status
- final decision reason code
- fetch/result source (`book`, `fallback`, or `missing`)
- execution revalidation outcome (`approved`, `rejected`, or `fallback`)

Historical paper session rows must remain readable if parity metadata is absent, and all new fields must be optional on read.

### Acceptance criteria
- parity-enabled paper sessions preserve enough data to explain changed outcomes
- audit rows remain readable and backward-compatible where possible

---

## 5. Status / Reporting

### Files
- reporting/status modules as needed

### Change
Expose parity mode in status/reporting so it is obvious whether a paper run was:
- logic-only
- live-parity revalidated
- parity fallback used

Reporting should reinforce the distinction between:
- normal paper mode as a strategy/logic lab
- parity mode as a paper execution-realism lab
- live as the actual trading adapter

### Acceptance criteria
- paper session/report clearly indicates parity mode state
- confusion between logic-only and parity-mode runs is reduced

---

## 6. Tests

### New tests required

#### A. Shared approval parity tests
Given identical inputs and prices:
- paper parity path and live execution revalidation path should produce the same approval decision

#### B. Price-drift rejection test
Example:
- signal generated at 1¢
- execution snapshot moves to 3¢
- hidden-gem edge collapses
- paper parity mode should reject, while logic-only paper mode would approve

#### C. Fallback behavior test
- execution-time bid/ask fetch unavailable
- confirm behavior matches config:
  - reject if `require_book_prices: true`
  - fallback if `fallback_to_signal_prices: true`

#### D. Audit persistence test
- verify original and execution snapshots are written when enabled

#### E. Backward-compatibility test
- parity mode disabled should preserve current paper flow
- older paper sessions without parity metadata should still load cleanly

#### F. Golden parity fixture test
- the same signal and book snapshot passed through paper parity mode and live revalidation should match on:
  - approval status
  - reason code
  - execution-price semantics

### Acceptance criteria
- tests clearly separate logic-only and parity-mode behavior
- tests validate config-driven switching
- tests verify parity on both outcome and reason-code behavior, not just final boolean approval

---

## 7. Reviewer Concerns to Watch For

These are the likely reviewer questions:

1. **Are we accidentally making paper too complex by default?**
   - Must be no. Default remains unchanged.

2. **Are we duplicating live logic in paper?**
   - Must be minimized. Shared helper preferred.

3. **Will this make historical paper sessions harder to read?**
   - Need backward-compatible audit strategy.

4. **Is this actually testing trading logic, or execution assumptions?**
   - Answer: parity mode is specifically execution-time decision parity, optional by design.

5. **Could this blur the line between strategy quality and execution quality?**
   - Only if audit is weak. That is why original vs execution snapshots are required.

---

## Rollout Plan

### Phase 1
- config block
- shared execution snapshot helper used by both paper and live
- paper-side optional execution revalidation
- audit fields for original vs execution snapshot
- tests
- explicit documentation that parity mode is paper-only and exists to model market movement before going live

### Phase 2
- cleaner normalized execution result schema

### Phase 3
- optional paper slippage / partial-fill simulation if desired later

---

## Recommended Implementation Order

0. Lock in the architectural rule: if a decision-prep behavior is needed by both paper and live, move it into shared code before parity mode depends on it
1. Add the small shared execution-snapshot helper
2. Refactor live to use the shared helper as the canonical execution-price normalization path
3. Define execution-price semantics explicitly in code/comments/tests
4. Add config support in `bot/config.py`
5. Wire optional revalidation into paper execution path using the shared helper
6. Add audit fields
7. Add tests
8. Update docs/status output

---

## Definition of Done

This spec is done when:

1. Paper supports a config-driven optional live-parity revalidation pass
2. Default paper behavior remains unchanged
3. Live and paper both derive shared execution-time pricing inputs from the same canonical helper
4. Parity-enabled paper uses execution-time price inputs before final approval
5. Audit logs clearly show original vs revalidated decision state
6. Matching inputs produce matching paper/live approval outcomes
7. Tests cover enabled, disabled, fallback, and drift scenarios
8. The docs clearly preserve the intended separation: normal paper tests logic, parity mode tests paper under live-style market movement, and live remains the real execution adapter

---

## Bottom Line

This change should make the system more useful in three distinct ways:

- **Logic-first paper mode** for fast strategy iteration
- **Parity paper mode** for pre-production validation against live-like execution-time conditions and market movement
- **Live trading** as the real adapter that acts on the API and carries trades through the real lifecycle

That gives us the best of both worlds without forcing one workflow to serve every purpose, and without confusing paper parity mode for live itself.