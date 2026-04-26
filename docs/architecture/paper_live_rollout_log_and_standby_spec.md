# Paper-First Rollout Log + Shared Standby Mode Spec

## Goal
Create a durable, reusable rollout pattern where new execution policies are designed for both paper and live from the beginning, implemented in paper first, then promoted into live without rediscovering how they work.

This spec covers two related ideas:
1. a **paper-first rollout log** for shared policy changes
2. a **shared capital-constrained standby mode** that can exist in both paper and live, while being implemented in paper first

## Design Principle
Paper mode is not a toy branch. It is the proving ground for live.

Because of that:
- new execution policies should be designed as shared policies
- paper should be the first implementation target
- live should be the second rollout target
- every such policy should leave an implementation log so future promotion is fast and reliable

## Part 1: Paper-First Rollout Log

## Purpose
When we build a paper-first feature, we should record:
- what the shared concept is
- what paper implementation exists today
- what live parity assumptions exist
- what still blocks live rollout
- where the code lives

That way we do not have to rediscover the design later.

## Proposed file
Create and maintain:

`docs/architecture/paper_live_rollout_log.md`

## Per-feature entry template
Each entry should include:

### Feature name
Example: `capital_constrained_standby_mode`

### Intent
What behavior this feature is meant to create in both paper and live.

### Shared design surface
Which shared-core or shared policy layer it belongs to.

### Paper implementation status
- not started / partial / complete
- key files changed
- tests added

### Live implementation target
- where it should hook in later
- what needs parity verification
- what assumptions may break in live

### Promotion blockers
Anything still preventing safe live rollout.

### Observability
What logs, metrics, state fields, or reports should prove the policy is working.

### Rollout recommendation
- paper-only
- paper-ready / live-design-ready
- ready for cautious live rollout
- fully live-ready

## Why this helps
This gives us a durable memory trail so promotion from paper to live becomes:
- faster
- safer
- less dependent on reconstructing old decisions

---

## Part 2: Shared Capital-Constrained Standby Mode

## Problem
When the system is unable to trade meaningfully because of:
- max position limits
- tradable balance limits
- exposure headroom limits
- repeated capital blockers

it may continue scanning even though it cannot act.

That creates:
- wasted scans
- noise
- unnecessary enrichment work
- weak paper realism if it keeps pretending to search while capital-constrained

## Goal
Define a shared policy called `capital_constrained_standby_mode` that can exist in both paper and live, but is first implemented in paper.

The bot should pause active execution scanning when meaningful trading capacity is not available, then resume when capacity has materially improved.

## Core Principle
The resume condition should not depend only on profit.

A losing resolution can still reduce exposure enough to restore useful trading capacity.
So the system should ask:

> Can I trade properly again?

not:

> Did I merely win money back?

## Shared Policy Name
`capital_constrained_standby_mode`

## Initial rollout plan
- design as shared policy now
- implement in paper first
- record assumptions in rollout log
- later promote into live if behavior proves good

## Entry Conditions
Enter standby mode when capital-constrained blockers persist.

### Implementation note on cycle semantics
For v1, a "scan" means a paper trading policy evaluation cycle, that is, one full paper loop pass that attempts normal scan/decision work and produces blocker counts.

### Canonical capital-blocking reason codes
For v1, standby entry only accumulates these capital-constrained blocker families:
- `max_positions`
- `tradable_balance`
- `capital_exposure`

These are derived from blocker/reason-code surfaces already emitted by the simulator/risk path.

### Suggested v1 entry rule
Enter standby if any capital-blocking reason family is present for `N` consecutive policy evaluation cycles.

Important:
- the streak is based on "any capital blocker present", not "same exact blocker repeated"
- if a cycle completes without a capital blocker, the streak resets

Recommended v1 default:
- `blocked_scan_threshold = 3`

## Behavior While in Standby
While in standby mode:
- stop normal market-fetch/scan work
- do not attempt normal trade placement
- continue lightweight resolution and resume checks
- record why standby is active

### Standby loop behavior in v1
Paper standby does NOT fully sleep forever.
It continues a lightweight cycle that:
- reevaluates whether positions have resolved
- reevaluates whether exposure has fallen enough
- reevaluates whether useful trade capacity is now sufficient

This is what allows standby to clear without resuming full scanning prematurely.

### Optional lightweight behavior
If desired later, standby mode can support a low-cost watchlist mode, but v1 should prefer simplicity.

## Resume Conditions
Resume only when meaningful trading capacity has improved.

### Important rule
Do **not** require strict positive cash recovery.
Otherwise losing resolutions could trap the bot in standby forever.

### Recommended v1 resume logic
Resume when the following boolean predicate is true:

```text
resume = (positions_resolved_enough OR exposure_reduction_enough) AND useful_capacity_enough
```

Where:
- `positions_resolved_enough` means resolved position delta since standby entry is at least `min_positions_resolved_to_resume`
- `exposure_reduction_enough` means exposure has fallen by at least `min_exposure_reduction_pct` relative to standby-entry exposure
- `useful_capacity_enough` means estimated useful trade capacity is at least `min_useful_trade_size_usd`

This precedence is intentional and should be preserved literally in implementations.

## Meaningful trading capacity
This should be defined in shared terms, not paper-only terms.

### v1 operational definition
For paper v1, meaningful trading capacity is represented by an estimated useful trade capacity computed from current tradable cash / available cash after existing constraints, and compared against a useful-size floor.

Candidate shared inputs over time:
- available / tradable cash above threshold
- open positions below threshold
- total exposure reduced enough
- estimated next approved trade size exceeds a configurable useful-size floor

In v1 paper implementation, the useful-size floor is the main operational gate.

## Recommended v1 defaults
```yaml
standby_mode:
  enabled: true
  blocked_scan_threshold: 3
  min_positions_resolved_to_resume: 2
  min_exposure_reduction_pct: 0.10
  min_useful_trade_size_usd: 5.0
```

Notes:
- `min_positions_resolved_to_resume = 2` is a good starting point
- this avoids waking up after a single tiny resolution
- useful-size threshold avoids resuming just to place tiny low-value trades

## Shared vs mode-specific responsibilities

### Shared policy layer should define
- standby state concept
- entry conditions
- resume conditions
- reasoning / blocker codes
- observability fields
- state-machine semantics for blocked streak accumulation, active standby, and resume clearing

### Paper implementation should prove first
- whether the policy reduces useless scans
- whether it improves trade quality after resume
- whether resume conditions are too strict or too loose

### Live implementation later should reuse
- same conceptual state machine
- same blocker/resume logic where possible
- live-specific account/order details only where necessary

## Observability
When active, the system should log:
- standby entered_at
- standby reason(s)
- blocked scan count
- unresolved position count at entry
- exposure at entry
- resume trigger reason
- exposure/capacity delta at resume

This should make future live rollout much easier.

## Suggested state fields
Could live in paper state first, then generalized later:
- `standby_active`
- `standby_entered_at`
- `standby_reason_codes`
- `standby_blocked_scan_count`
- `standby_unresolved_positions_at_entry`
- `standby_exposure_at_entry`
- `standby_available_cash_at_entry`
- `standby_last_resume_at`
- `standby_last_resume_reason`

## Rollout Recommendation
### Phase 1
- spec shared standby policy
- add rollout log structure
- implement standby in paper only
- test and observe behavior

### Phase 2
- review paper results
- refine resume thresholds
- decide whether watchlist mode is worth adding

### Phase 3
- promote shared standby behavior into live with mode-specific account/order integration

## Success Criteria
This feature is successful if:
- paper stops wasting scans while capital-constrained
- resume happens only after meaningful capacity improvement
- losing resolutions do not permanently trap the system
- future live rollout is easy because the shared policy and rollout log already exist

## Non-Goals
- not building a full scheduler/orchestrator redesign
- not building watchlist intelligence yet unless needed
- not forcing live rollout immediately

Paper first, shared by design, live later.
