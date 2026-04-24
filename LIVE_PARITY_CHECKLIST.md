# Live Parity Checklist

Purpose: make live trading a faithful, resilient implementation of the paper-tested logic.

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

## 2. Signal Input Parity

- [~] Paper evaluates signals from strategy snapshot in default mode
- [x] Live revalidates using current bid/ask before execution
- [x] Shared execution snapshot / pricing normalization helper exists and live uses it
- [x] Paper parity mode can simulate the same bid/ask revalidation pass as live before final approval
- [x] Paper parity mode stores both original signal snapshot and revalidated execution snapshot for apples-to-apples comparison

### Why this matters
A trade can pass in paper on stale snapshot assumptions but fail live once the real book is checked.

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
Right now differences may be intentional, but they still make parity harder to reason about.

---

## 5. Execution Parity

- [~] Paper has a dedicated execution adapter
- [~] Live has a dedicated execution adapter
- [x] Live revalidates before order placement
- [~] Normalize execution result schema so paper and live emit the same fields where possible
- [~] Record requested size, approved size, placed size, fill price, slippage estimate, and execution timestamp in the same shape
- [x] Both modes preserve decision reasoning alongside execution outcome

### Why this matters
If trade records differ too much, analysis becomes harder and parity regressions hide in data formatting.

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
- [~] Live and paper now share execution snapshot normalization, but live still needs richer persisted audit snapshots
- [ ] Add before/after snapshots for account state around order placement and reconciliation
- [ ] Add structured reason codes for live execution failures/rejections
- [ ] Add a parity report that compares paper-style expected fields vs live-recorded fields

### Why this matters
If live fails in weird ways, you need paper-grade visibility into why.

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

- [~] Unit tests now cover shared execution snapshot alignment and a golden same-snapshot paper/live decision case
- [ ] Fixture tests for hidden-gem logic in both modes
- [ ] Fixture tests for retrade/event-overlap logic in both modes
- [ ] Fixture tests for partial-fill and order-status transitions
- [ ] Fixture tests for restart/recovery flows
- [ ] Golden-file comparison of trade-history rows from paper vs live for equivalent scenarios

### Why this matters
Right now a lot of parity is inferred from code structure rather than proven by tests.

---

## 12. Production Readiness Priorities

### Highest priority
- [x] Extract shared execution snapshot / price normalization logic and make live use it first
- [x] Paper parity mode can simulate live revalidation with current bid/ask semantics
- [ ] Unify execution/audit row schema across paper and live
- [ ] Harden live order lifecycle handling: partials, rejects, cancels, stale orders
- [ ] Unify settlement lifecycle states and accounting shape
- [ ] Add restart/recovery parity tests

### Second priority
- [ ] Create config-driven parity mode with identical risk settings
- [ ] Add invariants and reconciliation alarms
- [ ] Add richer live audit snapshots

### Nice to have
- [ ] Optional paper simulation of partial fills and slippage
- [ ] Automated parity diff report after runs

---

## Bottom Line

Current state:
- Decision logic parity: **strong**
- Execution parity: **improving, with Phase 1 snapshot parity now in place**
- Lifecycle/accounting parity: **partial**
- Production resilience in live: **needs hardening**

Recommended framing:
- Paper is already a good decision lab
- Live is not yet a full paper-equivalent operational twin
- The main remaining work is around execution truth, reconciliation, lifecycle accounting, and restart safety
