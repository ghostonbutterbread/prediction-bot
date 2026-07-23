# Guarded Source-Router Shadow Lane

## Purpose

`shadow_source_router_no_price_guard` is a forward-paper comparator for the existing `shadow_source_router` control. It tests whether restricting the source-router policy to historically diagnostic `BUY_NO` price bands improves payout-aware outcomes without changing the control lane.

This is a **shadow-only hypothesis**, not a production or live-trading policy.

## Implementation

The lane is defined in:

```text
lanes/shadow_source_router_no_price_guard.yaml
```

Its committed settings are intentionally disabled:

```yaml
id: shadow_source_router_no_price_guard
type: source_router
source_wallet: stable_paper
source_role: baseline
input_source: shared_candidate_dataset
input_market_source: shared_market
enabled: false
parameters:
  hypothetical_notional_usd: 10.0
  allowed_actions: [BUY_NO]
  allowed_entry_price_ranges:
    - [0.60, 0.70]
    - [0.80, 0.90]
```

The corresponding code path is the source-router lane evaluator in `bot/paper_shadow_lanes.py`. It fail-closes unless all conditions hold:

1. the source-router decision is `BUY_NO`;
2. the decision has a valid selected-side / NO entry price;
3. that price falls within one configured inclusive band; and
4. ordinary lane input and shared-candidate integrity checks pass.

It does not alter `shadow_source_router`, paper balances, wallet state, order placement, or candidate collection.

## Comparison contract

When explicitly approved for a forward-paper comparison profile, run both lanes against the same shared collector snapshots:

```text
shadow_source_router                  # unchanged control
shadow_source_router_no_price_guard   # guarded comparator
```

Start a fresh cohort at enablement. Do not emit or backfill the guarded lane into a prior cohort.

The comparison must join only exact shared-candidate and snapshot identities. Missing or mismatched snapshots are not comparable and must fail closed.

## Measurement

For every overlapping candidate, report:

- candidate and snapshot coverage;
- control versus guarded action/side differences;
- decision-time selected-side ask/fill and hypothetical notional;
- independent-resolution match coverage;
- wins, losses, VOID exclusions, stake, payout, PnL, and ROI;
- integrity blocker counts.

Resolution/PnL requirements:

- outcome comes only from an independently maintained resolution artifact;
- lane provenance and `future_pnl_inputs` cannot settle a trade;
- ambiguous resolution matches fail closed;
- `VOID` produces `void_resolution` and is excluded from wins, losses, stake, payout, and PnL.

## Evidence status

Historical replay selected these bands as an **in-sample diagnostic hypothesis**. It may be used for deterministic side-by-side replay on the sanitized corpus, but it is not promotion evidence.

Promotion requires independently resolved, fresh, full-overlap forward-paper cohorts with the cost model and exposure policy declared in advance. The promotion gates in `docs/SOURCE_ROUTER_LANE_PROMOTION.md` remain authoritative.

## Lifecycle

- Keep this lane on `feature/source-router-ev-shadow` while evidence is immature.
- Merge it into `main` only after the predeclared forward comparison gate passes.
- Otherwise retain it as paper-only research or drop the isolated experiment commit.
