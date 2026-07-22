# Source-router Lane Promotion Gates

`source_router_sample_ge5_price_20_80_cap1` is a **shadow-only replay composition**. It never places orders or mutates paper accounting.

## Historical evidence

The replay-grade `joined_source_router_rows.jsonl` corpus produced 23 latest-per-market observations: 18 wins, 5 losses, $93.8967 PnL on $230 stake (40.82% ROI). This is hypothesis evidence only: selecting the earliest observation for the same markets produced 3.55% ROI.

## Non-negotiable prerequisites

A promotion candidate must have all of the following:

1. Collector-owned shared snapshots are fresh; snapshot-id mismatch fails closed.
2. Lane PnL is joined only to an independent resolution-feed record.
3. Every selected buy records a positive side-specific ask/fill, position size, source sample evidence, and shared candidate ID.
4. The resolved report has no unresolved or ambiguous resolution rows for the evaluation cohort.
5. Fees, slippage, partial fills, and compounding are either modeled or explicitly excluded from promotion metrics.

## Promotion sequence

1. Run the lane as forward shadow only, using a newly collected cohort; do not reuse historical snapshot decisions as a live result.
2. Freeze each cohort at decision time and run the central resolver after outcomes finalize.
3. Evaluate one exposure-capped row per market, with both earliest and latest decision selection reported.
4. Compare it against `control_stable`, `range_sample_ge5_cap1`, and `nws_only_cap1` on exactly the same candidate IDs.
5. Promote only after a predeclared forward sample and positive net performance after the selected cost model. Require zero integrity blockers.
6. First operational enablement remains paper-only and single-trade/small-notional. Actual live trading requires a separate explicit approval.
