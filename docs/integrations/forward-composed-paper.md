# Forward composed paper observer integration dossier

- **Status:** corrected after independent review blocker; disabled, pending fresh review (not a full pre-risk picker connection)
- **Owner:** forward-composed-paper task, isolated implementation agent
- **Branch / owning ref:** `feat/forward-composed-paper`
- **Base commit:** `14982f867bc324aae21bb1c2c8e396fb176b4dfe`
- **Intended integration target:** `beta` (parent owns independent review and decision)
- **Last updated:** 2026-10-04 (local PDT)
- **Latest immutable recovery checkpoint:** `baf3cdd062041282adaf414ba8c47ea0dba05a82` (`feat/forward-composed-paper`)
- **Feature implementation commit(s):** `baf3cdd062041282adaf414ba8c47ea0dba05a82`
- **Inspiration:** picker → strictly prior-settled Source Router → selected-side executable ask / fee-aware loss guard → synthetic Kelly/risk; existing `bot/paper_shadow_lanes.py`, `bot/composed_lane_wallet.py`, shared snapshot runtime.

## Intent and implemented contract

`bot/forward_composed_paper.py` pairs existing forward `control_stable`, `shadow_source_router`, and `shadow_fee_aware_loss_guard` lane rows on exact candidate ID, snapshot ID, run ID, market ID, **and observed time**. Stable BUY is the only action source. A strict direct-history Router BUY must confirm the same side; no side flips. The existing guard must return that side and a genuine selected-side ask/fee-aware quote basis. Missing, duplicate, mismatched, or unverified components are explicit SKIPs. The Router's trust score is never used as win probability; economics uses the stable model's YES probability (complemented for BUY_NO). The stable lane producer now retains question, exchange, event ticker, and route from the same decision-time signal for wallet projection. Older rows without them are coverage gaps, not fabricated inputs.

A separate manual `scripts/forward_composed_paper.py` command is **disabled by default** and reads only an explicitly provided full forward lane JSONL, current shared-publisher state JSON, and optionally a separately supplied exact-authority resolution JSONL. `--enable-paper-observer` must be explicit. It checks publisher identity and latest snapshot freshness before emitting a JSON report to stdout. No scheduler, runtime profile, exchange polling, collector, resolver, order, real-wallet, or raw archive change. Re-reading the entire append-only cohort rebuilds independent control/combined synthetic wallets chronologically; no wallet state is written. The shared-core decision calls real Kelly and risk seams; the synthetic account supplies available cash and per-event headroom, reserves stake, prevents duplicate open markets and rebuy after settlement, and releases capital only on a complete exact-identity authoritative later settlement (and no earlier than `outcome_known_at` when supplied). `VOID` releases reservation without economic P&L. Declared default model is 7% of positive winning profit; quote-based model P&L is labelled `quote_proxy_not_fill`, **never an observed fill**. `paper_parity_claim: false`, `promotion_eligible: false`.

Manual invocation once a fresh forward cohort and strict history are independently verified:

```bash
PYTHONPATH=. python3 scripts/forward_composed_paper.py \
  --lane-decision-path <forward-paper-lane-ledger.jsonl> \
  --shared-market-state-path <shared-market-runtime-state.json> \
  --resolution-path <separate-exact-authority-receipts.jsonl> \
  --enable-paper-observer
```

Omit `--resolution-path` to inspect only open synthetic reservations and paired decisions. Running without `--enable-paper-observer` returns `observer_disabled` without reading inputs. Redirect stdout **only** to an approved derived-report location, never a raw archive or real wallet.

## Evidence and review

- RED receipts: focused tests failed on missing module, absent CLI, missing stable wallet fields, reference-only control wallet opening, legacy wallet-input crash, and early/duplicate settlement.
- GREEN: `PYTHONPATH=. python3 -m unittest discover -s tests -p 'test_forward_composed_paper.py' -q` — 12 passed; `PYTHONPATH=. python3 -m unittest discover -s tests -p 'test_composed_lane_wallet.py' -q` — 4 passed.
- Full suite on the final code: `PYTHONPATH=. python3 -m unittest discover -s tests -q > /home/ryushe/.hermes/cache/scratch/forward-composed-full-suite.log 2>&1` — 1,375 tests, 7 skipped, OK (64.198s); log line 1361 and 1363. Rerun on the reviewed merge tree before integration.
- Independent read-only review of `916d0e6` found a paired-wallet blocker: a guard's recorded fill estimate above the genuine selected-side ask suppressed the control wallet but opened the combined wallet. RED regression: `test_control_uses_recorded_ask_when_guard_estimates_higher_entry` failed (`control_quote_unavailable` rather than `quote_proxy`) at .40 ask / .45 estimate. GREEN: control uses the proven ask .40, combined retains conservative estimate .45; 13 observer and 4 wallet tests passed. Full isolated suite on the correction: 1,376 tests, 7 skipped, OK (64.5s). `git diff --check` passed. Fresh independent review of the corrected commit is pending.
- Synthetic three-candidate fixture (outcomes not supplied): before/control recorded actions = three BUY_YES; after/composed = one BUY_YES, two SKIP (`router_disagrees`, `executable_side_ask_unavailable`). With $100 starting balance and fixed test Kelly/risk, quote-qualified control opens two synthetic reservations and combined opens one; **zero settled rows on both sides**. This is wiring/coverage, not profit evidence. Separate exact later YES fixture at $0.40, $10 stake, 7% profit fee gives quote-proxy model P&L +$13.95, not an actual fill.
- Merge base `git merge-base HEAD beta` at start: `14982f867bc324aae21bb1c2c8e396fb176b4dfe`. No merge/push/activation.

## Blockers and deferred work

1. **Pre-risk picker coverage:** the forward source adapter currently records stable **accepted** candidate actions as `control_stable`; its shadow consumer does not preserve every pre-risk picker BUY. This checkpoint intentionally does **not** claim the September 29 diagnostic's 65 BUYs or any all-picker coverage. The paired universe is exactly the recorded `control_stable` rows. To complete: introduce an explicit picker-intent capture before risk (including the collector-owned shared candidate path), route and guard it on the same snapshot, and test accepted versus rejected pre-risk buys end-to-end. Trigger after owner approves producer-seam expansion; run focused scanner/shared-consumer fixture and full suite. This blocks calling this the full requested forward connection.
2. **Strict history:** Router history admission repair is concurrently owned by `fix/source-router-unpriced-correctness`. This branch does not edit that code or integrate it. Direct-history provenance alone is an admission contract, not proof a deployed generation has sufficient samples or produces forward BUYs. Trigger a fresh strict-history consumer smoke on the reviewed combined tree before manual activation.
3. **Wallet parity:** the manual CLI constructs a fresh paper RiskManager, but does not synchronize its drawdown/loss-streak/cooldown state with each synthetic settlement; its event key falls back to contract ID when event ticker is missing; `market_family`/correlated family and persistent risk history are not fully represented. Independent exact-receipt provenance is caller-supplied (`authoritative`, complete identity, settlement time and row SHA); this observer does not validate a resolution generation's manifest. Quote alone has no fill or depth proof. Trigger wallet lifecycle parity tests and strict exact-resolution provenance verification before P&L or promotion claims. Do not interpret final balance as paper-account P&L.
4. **Forward evidence:** no fresh full-overlap independently settled combined cohort or result-based tuning exists. Trigger after a named forward cohort and separately authoritative resolutions exist; report overlap, missing intents/quotes, directional outcomes, quote-proxy only, then reconsider activation/promotion.

## Interruption / resume handoff

- **Owning feature branch/ref:** `feat/forward-composed-paper`
- **Previous immutable recovery checkpoint:** `baf3cdd062041282adaf414ba8c47ea0dba05a82`; the correction is being committed with this dossier update. Record and verify its exact SHA in the next dossier-only handoff.
- **Exact resume point:** review the full diff and this dossier; rerun focused/full on the exact commit, then decide whether to expand the producer to genuine pre-risk intents or retain as a strictly narrower diagnostic. Parent controls integration.
- **Working-tree state at handoff:** clean after commit (verify).

## Decision gates

- **Integration:** independent review of exact branch tree and combined-beta test; remove this branch-local dossier from integration target.
- **Activation / cohort:** explicit user/owner authorization; verified strict history publication, current shared snapshot, risk-state contract, forward data compatibility; no service/profile is enabled by this commit.
- **Promotion:** independent fresh full-overlap settled outcomes with real quote/fill provenance, capital/risk parity and drawdown; current fixture cannot meet this.

## Decision record

- 2026-10-04 PDT — disabled read-only adapter and synthetic-wallet checkpoint; intentionally blocked on pre-risk capture and forward evidence.
- 2026-10-04 PDT — independent review blocked code-only integration over control ask versus guard estimated-price mismatch; focused RED/GREEN fix and full suite passed, awaiting corrected-tree review.
