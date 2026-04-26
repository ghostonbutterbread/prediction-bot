# Event-Aware Retrade v2 Spec

## Goal
Improve the current event-aware retrade system so it better models real execution quality, market overlap, and live order lifecycle. The aim is to move from a solid risk-control baseline to a more realistic portfolio-building engine.

## Current State
v1 already provides:
- shared-core event-aware retrade decisioning
- event exposure cap
- max positions per event
- stronger retrade threshold
- size decay and event headroom clipping
- duplicate market blocking
- opposite-side same-event blocking
- live pre-placement revalidation
- event-aware reporting

## Why v2
The current implementation is safe enough for practical use, but three areas are still under-modeled:
1. retrade economics are mostly fee-aware, not truly slippage-aware
2. same-family price-improvement guard is configured but not enforced
3. live order lifecycle is simplified, especially resting vs filled exposure handling

## Product Direction
The bot should keep acting like a careful trader scaling into conviction, not like a contract collector.

That means:
- only add to an event when the new entry is still meaningfully attractive after realistic fill costs
- require a better reason to re-enter highly overlapping same-family markets
- distinguish pending risk from filled risk more clearly in live mode

## V2 Workstreams

### Workstream 1: Real slippage-aware retrade economics

#### Problem
Current retrade viability uses fee-aware net edge, but slippage estimates are weak because contexts often lack meaningful fill-cost inputs.

#### Goal
Use realistic expected execution cost when judging whether a retrade still makes sense after decay and clipping.

#### Requirements
- thread liquidity/order-book context into shared trade contexts for both paper and live
- estimate slippage using actual intended size, not zero-size placeholders
- re-evaluate net edge after:
  - entry price
  - size decay
  - event headroom clipping
  - expected slippage
  - fees
- reject retrades if post-cost net edge falls below threshold
- optionally add minimum expected net profit in dollars for retrades

#### Candidate interface additions
Add to `TradeContext.metadata` or source context:
- `liquidity`
- `best_yes_ask`
- `best_no_ask`
- `best_yes_bid`
- `best_no_bid`
- `estimated_fill_price`
- `estimated_slippage`

#### Desired behavior
If a retrade only looked good before realistic fill costs, it should be rejected.

---

### Workstream 2: Enforce same-family price-improvement guard

#### Problem
We have config knobs for price-improvement on same-family retrades, but they are not actively enforced.

#### Goal
If the bot wants to retrade within the same event family, it should need a better price or a clearly better reason.

#### Requirements
- define same-family grouping deterministically
- for retrades within the same family, optionally require:
  - better entry price by `price_improvement_ticks`, or
  - materially better net edge
- if guard is enabled and threshold is not met, reject retrade
- include rejection reason in decision trace

#### Initial v2 rule
If `require_price_improvement_for_same_market_family = true`:
- and there is an unresolved same-family position already
- then new trade must improve effective entry price by at least `price_improvement_ticks`
- otherwise reject

This should be conservative and explainable.

---

### Workstream 3: Better live order lifecycle modeling

#### Problem
Live flow still materializes placed orders as open positions quickly, which simplifies reality. That is workable, but not ideal.

#### Goal
Distinguish:
- resting orders
- partially filled orders
- filled positions

so event exposure accounting is more realistic.

#### Requirements
- represent live pending orders distinctly from filled positions
- track pending event exposure separately from filled event exposure
- reconciliation should move exposure cleanly between pending and filled states
- reporting should distinguish:
  - pending same-event exposure
  - filled same-event exposure
- same-event headroom checks should include both, but reasoning should show which is which

#### Desired behavior
The bot should know the difference between:
- “I already own this event”
- “I have a resting order trying to buy more of this event”

---

## Optional Workstream 4: Better overlap geometry

#### Problem
v1 overlap policy is intentionally simple and deterministic, but some same-event contracts may still overlap in a way that deserves stronger penalties.

#### Goal
Improve same-event overlap decisions beyond exact family matching.

#### Requirements
- keep deterministic rules
- avoid fuzzy heuristics
- define contract-neighborhood distance where possible
- optionally apply penalties instead of pure rejection for moderately overlapping contracts

#### Note
This is useful, but less urgent than slippage and live lifecycle.

---

## Recommended Implementation Order
1. real slippage-aware retrade economics
2. same-family price-improvement enforcement
3. better live order lifecycle modeling
4. optional overlap geometry improvements

## Suggested v2 Defaults
```yaml
risk:
  min_retrade_net_edge: 0.005
  require_price_improvement_for_same_market_family: true
  price_improvement_ticks: 0.03
  min_retrade_expected_profit_usd: 0.25
```

## Reporting Additions
Add these where practical:
- retrades rejected for slippage erosion
- retrades rejected for missing price improvement
- pending event exposure vs filled event exposure
- average effective execution cost on retrades

## Success Criteria
v2 is successful if:
- retrade approvals drop for weak late entries
- live and paper stay aligned more often on marginal trades
- reports make concentration and execution quality easier to trust
- behavior feels more like deliberate scaling than repeated nibbling

## Non-Goals
Not trying to build a full market-making engine or advanced portfolio optimizer yet.
This is still a disciplined directional trading system with better event-aware scaling.
