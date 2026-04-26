# Live Parity Checklist

Purpose: make paper parity mode a faithful pre-live execution-realism lab, while keeping live trading as the real adapter and preserving normal paper mode as the simpler logic lab.

## Status Key
- [x] Implemented / largely aligned
- [~] Partial / needs hardening
- [ ] Missing

---

## 1. Shared Decision Brain

- [x] Shared strategy engine used by both paper and live
- [x] Shared `build_trade_decision()` path used by both paper and live
- [x] Shared threshold logic: min edge, min confidence, max entry price
- [x] Shared retrade / event overlap gating
- [x] Shared Kelly sizing path
- [x] Shared risk-policy check hook
- [x] Hidden gem logic shared across both modes (`<= 5¢`, min edge, probability multiple)

### Notes
This is the strongest part of parity right now. Paper tuning is meaningful because the core trade approval logic is shared.

---

## Framing

- Normal paper mode is the strategy and logic lab.
- Parity mode is still paper, but revalidates against live-style execution-time market movement.
- Live trading is the real adapter that interacts with the API and carries trades through the real lifecycle.

The checklist below should be read through that lens: parity work primarily upgrades paper, while shared helpers and audit alignment exist to keep paper's revalidation semantics anchored to real live behavior.

---

## 2. Signal Input Parity

- [~] Paper evaluates signals from strategy snapshot in default mode
- [x] Live revalidates using current bid/ask before execution
- [x] Shared execution snapshot / pricing normalization helper exists and live uses it
- [x] Paper parity mode can simulate the same bid/ask revalidation pass as live before final approval
- [x] Paper parity mode stores both original signal snapshot and revalidated execution snapshot for apples-to-apples comparison

### Why this matters
A trade can pass in normal paper on stale snapshot assumptions but fail once parity mode rechecks the real book shape. That is exactly the pre-live edge case parity mode is meant to surface.

---

## 3. Account State Parity

- [x] Both modes build a shared `AccountState`
- [x] Both modes expose available cash, reserved capital, exposure, open position count
- [~] Paper account state is derived from simulator state
- [~] Live account state is derived from exchange truth + local reconciliation
- [ ] Add parity tests to ensure the same open positions / reserved capital produce equivalent `AccountState` semantics in both modes

### Why this matters
Even with shared decision logic, different account-state construction can lead to different approvals or sizes.

---

## 4. Risk Policy Parity

- [x] Both modes use the same `RiskManager` class
- [~] Paper and live intentionally use different presets
- [ ] Make all live-vs-paper risk differences explicitly visible in config/docs
- [ ] Add a "parity mode" config where live and paper can run with identical risk settings for direct comparison
- [ ] Add fixture tests comparing paper and live decisions under identical account/risk inputs

### Why this matters
Right now differences may be intentional, but they still make paper parity results harder to reason about before going live.

---

## 5. Execution Parity

- [x] Paper has a dedicated execution adapter
- [x] Live has a dedicated execution adapter
- [x] Live revalidates before order placement
- [~] Paper/live rows now share a meaningful overlap and can be normalized through `bot/parity_audit.py`, but the canonical write-side contract is still underspecified
- [~] Requested/approved/placed/filled sizing fields are partially aligned, but not yet enforced as a single required row shape across both modes
- [x] Both modes preserve decision reasoning alongside execution outcome

### Why this matters
The repo now has the beginnings of a shared report surface, but write-time row semantics still need cleanup so parity regressions show up as behavior differences rather than formatting drift.

---

## 6. Order Lifecycle / Fill Handling

- [~] Paper assumes simplified fills
- [~] Live tracks open orders and partial fills
- [ ] Simulate partial-fill lifecycle in paper, at least optionally
- [ ] Add explicit live handling for stale orders, canceled orders, rejected orders, and partial-fill updates
- [ ] Confirm live trade history rows distinguish requested vs filled exposure correctly
- [ ] Add retry / reconciliation rules for transient exchange/API inconsistency

### Why this matters
This is one of the largest real-world gaps between paper and live.

---

## 7. Reconciliation Parity

- [~] Live has exchange-truth reconciliation on connect / refresh
- [~] Paper reconstructs from saved session state
- [ ] Define a shared reconciliation contract: positions, resting orders, reserved capital, available cash, trade history rows
- [ ] Add parity tests for startup recovery scenarios
- [ ] Add tests for "bot restarts with unresolved positions/orders" in both modes

### Why this matters
Production readiness depends heavily on safe restarts and correct recovery.

---

## 8. Settlement / Resolution Parity

- [x] Paper has strong resolver/accounting flow
- [~] Live has settlement/reconciliation logic but thinner lifecycle coverage
- [ ] Unify resolution event schema across paper and live
- [ ] Ensure both modes distinguish: open, closed-unsettled, settled, canceled, partially-filled, failed
- [ ] Reuse shared accounting helpers for realized and unrealized P&L everywhere possible
- [ ] Add parity tests for YES and NO outcomes, fees, and unsettled markets

### Why this matters
A production bot needs trustworthy post-trade accounting, not just good entries.

---

## 9. Audit Trail / Observability

- [x] Paper has strong audit trail
- [~] Live logs lifecycle and trade history, but not as richly as paper
- [~] Live and paper share execution snapshot normalization, but live still needs richer persisted audit snapshots and stricter row invariants
- [ ] Add before/after snapshots for account state around order placement and reconciliation
- [~] Structured reason codes exist in several paths, but they are not yet normalized into one canonical execution/audit schema
- [~] A first parity report surface exists (`bot/parity_audit.py`, `scripts/parity_viewer.py`), but it still needs richer joins, diff summaries, and stronger missing-field handling

### Why this matters
If live fails in weird ways, you need paper-grade visibility into why — and that now means hardening the row contract and the parity report, not just adding one from scratch.

---

## 10. Failure Handling / Resilience

- [~] Live has some reconciliation and sync safeguards
- [ ] Add explicit handling for exchange timeouts, stale order books, duplicate submissions, and idempotency
- [ ] Add safe retry strategy for non-terminal exchange errors
- [ ] Add kill-switch behavior for repeated reconciliation mismatches
- [ ] Add invariant checks: reserved capital >= 0, available cash >= 0, open order + open position exposure matches risk state
- [ ] Add chaos-style tests for restart/reconnect during open orders

### Why this matters
This is the difference between "works" and "production ready".

---

## 11. Testing Gaps

- [~] Unit tests cover shared execution snapshot alignment, parity proofs, recovery parity, and parity audit normalization, but lifecycle/report assertions are still thin
- [ ] Fixture tests for hidden-gem logic in both modes
- [ ] Fixture tests for retrade/event-overlap logic in both modes
- [ ] Fixture tests for partial-fill and order-status transitions
- [ ] Fixture tests for restart/recovery flows under richer live order states
- [ ] Golden-file comparison of trade-history rows from paper vs live for equivalent scenarios

### Why this matters
A lot of core parity is now proven, but row-contract and lifecycle parity are still under-tested.

---

## 12. Production Readiness Priorities

### Highest priority
- [x] Extract shared execution snapshot / price normalization logic and make live use it first
- [x] Paper parity mode can simulate live revalidation with current bid/ask semantics
- [ ] Unify the execution/audit row schema across paper and live at write time
- [ ] Harden the existing parity diff/report layer so it highlights schema gaps and behavior deltas clearly
- [ ] Harden live order lifecycle handling: partials, rejects, cancels, stale orders
- [ ] Unify settlement lifecycle states and accounting shape
- [~] Restart/recovery parity tests exist, but they still need broader live-lifecycle coverage

### Second priority
- [ ] Create config-driven parity mode with identical risk settings
- [ ] Add invariants and reconciliation alarms
- [ ] Add richer live audit snapshots and account-state before/after captures

### Nice to have
- [ ] Optional paper simulation of partial fills and slippage
- [ ] Automated parity diff report after runs with artifact export / golden comparisons

---

## Bottom Line

Current state:
- Normal paper mode: **good logic lab**
- Parity paper mode: **becoming a strong execution-realism lab**
- Live execution adapter: **real, but still needs operational hardening outside the parity lane**

Recommended framing:
- Normal paper tests logic and market selection
- Parity mode tests whether paper decisions still hold once the market moves like live
- Live remains the real adapter, with separate operational-hardening work still to do

For a practical pre-live summary, first-live risk caps, and known production gaps, see:
- `docs/LIVE_CANARY_READINESS.md`
