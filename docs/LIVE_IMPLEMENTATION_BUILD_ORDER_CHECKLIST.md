# Live Implementation Build Order Checklist

_Last updated: 2026-04-28_

## Purpose

This doc turns the current strategy/spec set into a practical implementation order.

It is the execution checklist that sits on top of:
- `docs/OVERALL_DIRECTION_SPEC.md`
- `docs/LIVE_LIFECYCLE_HARDENING_SPEC.md`
- `docs/LIVE_DEPLOYMENT_READINESS_PLAN.md`
- `docs/LIVE_CANARY_READINESS.md`
- `LIVE_PARITY_CHECKLIST.md`
- `docs/EXECUTION_AUDIT_ROW_SCHEMA_SPEC.md`

Use this when the question is:
- what do we build first?
- what belongs on the next branch?
- what should wait?
- what code areas and tests should each phase touch?

---

## Ground rule

Do **not** mix these into one muddy pass:
- Prediction Lab branch cleanup
- live lifecycle hardening
- deferred parity polish
- canary rollout work

The intended order is:
1. checkpoint/merge the current branch cleanly
2. do the next live-hardening branch
3. run the supervised canary with strict caps
4. return for deferred parity/reporting follow-ups as needed
5. only then discuss true deployment

---

## Phase 0 — Branch checkpoint and pre-implementation cleanup

### Goal
Start the next live-hardening branch from a clean, intentional baseline.

### Why first
Both `docs/OVERALL_DIRECTION_SPEC.md` and `docs/LIVE_LIFECYCLE_HARDENING_SPEC.md` assume the current branch should be checkpointed before deeper live work continues.

### Do now
- [ ] Commit or intentionally snapshot the current `feature/prediction-lab` / parity groundwork state.
- [ ] Make sure current docs are aligned:
  - [ ] `LIVE_PARITY_CHECKLIST.md`
  - [ ] `docs/LIVE_PARITY_IMPLEMENTATION_BRIEF.md`
  - [ ] `docs/LIVE_LIFECYCLE_HARDENING_SPEC.md`
  - [ ] `docs/LIVE_DEPLOYMENT_READINESS_PLAN.md`
- [ ] Confirm the next branch is explicitly framed as **live lifecycle hardening**, not general parity expansion.
- [ ] Preserve deferred parity items as intentional follow-ups, not forgotten work.

### Main files likely touched
- docs only, branch hygiene, commit structure

### Exit criteria
- [ ] Branch is clean enough to commit/merge.
- [ ] Docs tell a consistent story.
- [ ] Next branch scope is explicit.

---

## Phase 1 — Parity/operator visibility hardening (Mind first)

### Goal
Make the existing parity/reporting layer good enough to guide the live hardening work.

### Why before deeper live changes
If the reporting surface is too thin, live fixes will be harder to verify and explain.

### Build items
- [ ] Harden parity diff/report summaries so they clearly surface:
  - [ ] schema gaps
  - [ ] lifecycle contradictions
  - [ ] original-vs-revalidated reason-code deltas
  - [ ] original-vs-execution snapshot deltas
  - [ ] snapshot-source breakdowns (`book`, `fallback`, `missing`, `unknown`)
  - [ ] invalid-contract row counts / top issue breakdowns
- [ ] Add exportable comparison artifacts for longer parity runs.
- [ ] Improve handling of missing-field / partial-row cases.
- [ ] Make paper/live comparison output easier to inspect without reading raw rows manually.

### Main code targets
- `bot/parity_audit.py`
- `scripts/parity_viewer.py`
- report/export helpers if needed

### Main test targets
- parity report / diff tests
- fixture tests for missing/invalid/contradictory rows
- normalized comparison tests for equivalent paper/live scenarios

### Suggested test files
- `tests/test_parity_audit.py`
- new report/diff-focused tests if needed

### Exit criteria
- [ ] The parity surface is more than a debug viewer.
- [ ] Drift causes are easier to classify:
  - [ ] logic drift
  - [ ] risk drift
  - [ ] execution drift
  - [ ] lifecycle/reconciliation drift
- [ ] The output is strong enough to support the next live-hardening pass.

---

## Phase 2 — Canonical live lifecycle state machine

### Goal
Make live order and position state transitions explicit, canonical, and hard to misinterpret.

### Build items
- [ ] Harden live handling for canonical states:
  - [ ] `rejected`
  - [ ] `failed`
  - [ ] `placed`
  - [ ] `partial`
  - [ ] `filled`
  - [ ] `canceled`
  - [ ] `stale`
  - [ ] `resolved`
- [ ] Ensure lifecycle-state mapping is explicit and consistent:
  - [ ] `placed_open`
  - [ ] `partial_open`
  - [ ] `filled_open`
  - [ ] `canceled_unfilled`
  - [ ] `canceled_partial`
  - [ ] `stale_open_order`
  - [ ] `resolved_position`
  - [ ] stage-specific reject/fail states where appropriate
- [ ] Tighten row semantics for:
  - [ ] `requested_size`
  - [ ] `approved_size`
  - [ ] `placed_size`
  - [ ] `filled_size`
  - [ ] `remaining_size`
  - [ ] `reserved_capital`
- [ ] Make sure a resting order is never treated as a filled position.
- [ ] Make sure a placed-but-unfilled order is never written as `filled`.

### Main code targets
- `bot/live_execution.py`
- `bot/trade_audit.py`
- `bot/runner.py`
- related live adapter/state helpers

### Main test targets
- partial → fill
- partial → cancel
- placed → stale
- rejected-before-placement
- failed-after-submission-attempt
- canonical lifecycle-state/row-shape assertions

### Suggested test files
- `tests/test_live_execution.py`
- `tests/test_recovery_parity_edges.py`
- new lifecycle-transition tests if needed

### Exit criteria
- [ ] Lifecycle transitions are explicit in code and tests.
- [ ] Row/accounting semantics remain consistent across those transitions.
- [ ] The most important messy live states are no longer implicit or ambiguous.

---

## Phase 3 — Reconciliation contract and exchange-truth enforcement

### Goal
Make reconciliation first-class and make exchange truth reliably override local optimism.

### Build items
- [ ] Define a clearer reconciliation contract for:
  - [ ] open positions
  - [ ] resting orders
  - [ ] filled exposure
  - [ ] pending exposure
  - [ ] reserved capital
  - [ ] available cash
  - [ ] local-row corrections when exchange truth disagrees
- [ ] Implement mismatch severity classes:
  - [ ] low → log only
  - [ ] medium → correct and continue
  - [ ] high → block/pause and alert
- [ ] Persist useful reconciliation snapshots/events.
- [ ] Make reconciliation part of execution flow, not optional cleanup.
- [ ] Tighten duplicate-intent / uncertain-placement / degraded-refresh behavior.

### Main code targets
- `bot/live_execution.py`
- `bot/live_sync.py`
- `bot/runner.py`
- reconciliation-related helpers/adapters

### Main test targets
- local `placed` but exchange `partial`
- local open order but exchange `canceled`
- duplicate active intent collision
- degraded refresh / failed refresh
- restart with unresolved order + exchange truth disagreement

### Suggested test files
- `tests/test_recovery_parity.py`
- `tests/test_recovery_parity_edges.py`
- `tests/test_live_execution.py`
- new reconciliation mismatch tests if needed

### Exit criteria
- [ ] Exchange truth clearly wins when available.
- [ ] Reconciliation produces durable, explainable outcomes.
- [ ] Severe mismatches can halt live execution conservatively.

---

## Phase 4 — Runtime invariants, safety pause, and failure policy

### Goal
Make the bot loud and conservative when state becomes contradictory or unsafe.

### Build items
- [ ] Expand runtime invariant coverage for:
  - [ ] reserved capital never negative
  - [ ] available cash never negative
  - [ ] open order exposure + open position exposure reconcile to risk/account state
  - [ ] canceled partials preserve filled exposure history correctly
  - [ ] impossible lifecycle/accounting combinations are flagged
- [ ] Add or harden safety-pause behavior for:
  - [ ] repeated reconciliation mismatches
  - [ ] repeated non-terminal exchange/API failures
  - [ ] unresolved degraded state
  - [ ] negative or impossible accounting state
- [ ] Define safe retry policy for non-terminal exchange/API failures.
- [ ] Clarify when retry is allowed vs when the bot must stop trying.

### Main code targets
- `bot/live_execution.py`
- `bot/runner.py`
- `bot/trade_audit.py`
- risk / pause / alert hooks

### Main test targets
- invariant failure behavior
- repeated-failure pause behavior
- safe retry vs hard stop behavior
- degraded-state blocking

### Exit criteria
- [ ] Unsafe state is hard to miss.
- [ ] The bot stops conservatively instead of thrashing.
- [ ] Retry behavior is explicit and tested.

---

## Phase 5 — Restart/reconnect chaos coverage

### Goal
Prove that the bot stays conservative and legible across restarts with unresolved live state.

### Build items
- [ ] Add restart/reconnect tests for:
  - [ ] open resting orders
  - [ ] partial fills
  - [ ] canceled orders discovered after restart
  - [ ] exchange/local disagreement at startup
  - [ ] blocked/degraded/safe startup classification
- [ ] Ensure startup performs reconcile-before-trade in live mode.
- [ ] Ensure new live trading is blocked until severe uncertainty is cleared.

### Main code targets
- startup/recovery paths in `bot/runner.py`
- live recovery/reconciliation helpers
- `bot/live_execution.py`

### Main test targets
- restart/reconnect chaos-style cases
- startup classification cases
- reconciliation-before-new-order assertions

### Exit criteria
- [ ] Restart behavior is conservative and explainable.
- [ ] The bot does not resume optimistic trading into unresolved state.
- [ ] Recovery behavior is strong enough for supervised repeatability.

---

## Phase 6 — Settlement/accounting normalization

### Goal
Bring live settlement/accounting closer to paper-grade trustworthiness.

### Build items
- [ ] Unify settlement/resolution semantics around canonical:
  - [ ] `status`
  - [ ] `lifecycle_state`
  - [ ] `outcome`
- [ ] Ensure `outcome` means market-outcome truth (`YES` / `NO`), not bot-relative win/loss.
- [ ] Reuse shared accounting helpers wherever possible.
- [ ] Improve realized/unrealized P&L consistency.
- [ ] Add better edge-case handling for unsettled/closed/resolved states.

### Main code targets
- `bot/runner.py`
- `bot/trade_audit.py`
- settlement/resolution helpers

### Main test targets
- YES/NO outcomes
- fees
- unsettled markets
- resolved rows after prior open/partial/canceled states

### Exit criteria
- [ ] Live accounting is much more explainable post-settlement.
- [ ] Resolved rows match canonical semantics.
- [ ] Post-trade audit interpretation becomes easier and safer.

---

## Phase 7 — Config clarity and identical-risk comparison lane

### Goal
Make live-vs-paper differences explicit so comparisons are interpretable.

### Build items
- [x] Document intentional live-vs-paper risk preset differences clearly.
- [x] Add a clean identical-risk comparison mode/config path.
- [x] Add fixtures proving equivalent paper/live decisions under intentionally matched inputs.
- [x] Make reports/status surfaces show when a run is:
  - [x] normal paper
  - [x] parity paper
  - [x] identical-risk comparison
  - [x] live

### Main code/doc targets
- config docs
- parity config surface
- reporting/status surfaces

### Main test targets
- identical-risk decision fixtures
- mode/report labeling checks

### Exit criteria
- [x] Humans can tell whether a difference is expected or suspicious.
- [x] Paper/live comparison becomes more apples-to-apples when desired.

---

## Phase 8 — Supervised canary execution

### Goal
Prove the hardened system can touch real money conservatively.

### Preconditions
- [x] Phases 1–7 are far enough along that live behavior is explainable.
- [ ] The canary notes in `docs/LIVE_CANARY_READINESS.md` are still satisfied.

### Readiness checklist
- [x] Add read-only `canary-preflight` command that only loads static config and validates readiness.
- [x] Keep `config.live_supervised.yaml` fail-closed with `trading.enabled: false` and tiny explicit caps.
- [x] Block whole-percent risk mistakes such as `daily_loss_limit_pct: 35`.
- [x] Verify preflight does not instantiate the bot, connect exchanges, load `.env`, or apply env overrides.

### Canary checklist
- [ ] Use a tiny bankroll (for example `$100`).
- [ ] Use tiny position sizes (for example `$1–$5`).
- [ ] Keep hard exposure and daily-loss caps.
- [ ] Keep paper running in parallel.
- [ ] Manually review first live orders.
- [ ] Check exchange truth directly after first live order.
- [ ] Review local audit row vs exchange truth immediately.
- [ ] Review reconciliation behavior after a non-trivial session.

### Exit criteria
- [ ] First live sessions are boring in the best way.
- [ ] No surprising lifecycle/accounting contradictions appear.
- [ ] Manual review supports trust rather than revealing ambiguity.

---

## Phase 9 — Supervised repeatability and limited autonomy

### Goal
Earn confidence through repeated calm behavior before discussing unattended deployment.

### Build/ops items
- [ ] Run multiple supervised live sessions across multiple restarts.
- [ ] Review post-run artifacts for each session.
- [ ] Confirm safety-pause behavior works under degraded conditions.
- [ ] Confirm reconciliation remains trustworthy after real order history accumulates.
- [ ] Only then allow longer supervised runs with limited babysitting.

### Readiness/evidence checklist
- [x] Add read-only repeatability report tooling for supervised canary artifacts.
- [x] Keep the report local-artifact-only: no daemon start, no exchange connection, no bot instantiation, no `.env`, and no credential/env overrides.
- [x] Fail closed when fewer than two supervised sessions or required artifacts are present.
- [x] Check reviewed sessions for lifecycle/accounting contradictions, degraded reconciliation indicators, active safety pauses, and direct exchange-truth reconciliation fields when available.
- [ ] Capture multiple real supervised canary sessions with startup, reconciliation, and shutdown artifacts.
- [ ] Review the repeatability report output after each supervised session batch.
- [ ] Do not treat report readiness as permission for unattended live operation; it only creates evidence for a limited-autonomy discussion.

### Exit criteria
- [ ] The bot survives reality repeatedly, not just once.
- [ ] Evidence is sustained, not anecdotal.
- [ ] There is a credible case for discussing true deployment.

---

## Deferred until after the live-hardening branch

Do not let these sprawl into the earliest live-hardening phases unless needed directly:
- [ ] optional paper partial-fill/slippage simulation
- [ ] broad UI/dashboard overhaul
- [ ] microstructure realism beyond current deployment needs
- [ ] strategy expansion unrelated to live correctness

These remain useful, but they are not the gating path to deployment trust.

---

## Recommended immediate next move

If starting implementation now, the best order is:
1. **Phase 0** — checkpoint current branch cleanly
2. **Phase 1** — parity/operator visibility hardening
3. **Phase 2** — canonical live lifecycle state machine
4. **Phase 3** — reconciliation contract and exchange-truth enforcement
5. **Phase 4** — runtime invariants / safety pause / retry policy
6. **Phase 5** — restart/reconnect chaos coverage
7. **Phase 6** — settlement/accounting normalization
8. **Phase 7** — config clarity + identical-risk comparison lane
9. **Phase 8** — supervised canary
10. **Phase 9** — supervised repeatability / limited autonomy

---

## Bottom line

This is the implementation spine from:
- parity-capable and canary-capable

to:
- operationally trustworthy
- deployment-legible
- and eventually ready for true deployment

The central rule is simple:

**First make live behavior understandable. Then make it safe under stress. Then prove it repeatedly.**
