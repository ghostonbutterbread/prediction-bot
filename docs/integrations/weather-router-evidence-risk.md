# Weather Router evidence and risk repair

- Owner/task: Hermes / `t_daf9352a`.
- User scope: Discord thread `1561068455967457281`, message `1561285644423004233`: patch location correctness, probability evidence, and Router source-strength sizing. User will identify the existing small-profit lane; do not replace it.
- Branch/worktree: `fix/weather-router-evidence-risk`, `/home/ryushe/worktrees/prediction-bot-weather-router-evidence-risk`.
- Base: `a0e5f4decadb266b4f87a071887456997439272d` (fetched `origin/beta`). Target: **beta**, not main. Existing repository main-oriented topology guidance predates the established beta integration workflow; this task does not promote stable/main or change runtime.
- Status: preflight/design; no implementation yet.

## Evidence and intended changes

1. `weather_pro.py` resolves NWS points from city-center coordinates while separately attaching a configured station label. A public read of the CLILAX October 1 contract confirms a station-specific target; NOAA station KLAX geometry differs from the city point. Resolve forecast query location from the applicable station, retain truthful location provenance, avoid cross-location cache reuse and never silently fall back from a promised station forecast to city-center data. Preserve unknown/inferred authority as unknown. Test actual forecast consumers, not just label serialization.
2. The probability pipeline uses hardcoded forecast-distance probability steps, and Router research selects direction using source history but sizes with a stable probability. Inspect existing training and immutable evidence first. Reuse existing calibrated/empirical components if supportable; do not fabricate forecast-error distributions or convert rank/source accuracy directly into contract odds. Define the smallest evidence-backed repair after this preflight. No new training service or broad model platform. If compatible empirical training evidence is missing, record that as an explicit unfinished acceptance gate rather than claim calibrated odds.
3. Carry outcome-free Router evidence to actual wallet sizing and make an explicit opt-in paper experiment bound exposure by evidence strength. Preserve no-config/control behavior and YES-probability semantics; do not enable a lane or discard risk/accounting guards. Exercise weak versus stronger evidence through the real wallet and existing persistence consumer.

## Acceptance and non-goals

- RED/GREEN regression receipts for concrete repaired boundaries; focused tests plus full isolated suite, with base reproduction for unrelated failures.
- Positive producer -> composition -> actual Kelly/risk/wallet fixture and unchanged-control negatives; independently reviewed exact implementation/combined tree before integration.
- No live trades, balances, external account access, service/timer changes, collector restart, runtime configuration edits, or activation.
- No historical raw rewrites, fabricated backdated station forecasts, full replay or giant archive scan. Historical profitability remains unproven.
- Fee-model correction and user-selected payout-filter composition are not silently added to this patch.

## Verification and resume

- Fetched beta is clean at the base above; task worktree created from it.
- Verified test interpreter: `/home/ryushe/projects/prediction-bot-beta-integration/.venv/bin/python`; installed Kalshi SDK 3.9.0/httpx 0.28.1/pydantic 2.12.5 match exact manifest pins. Use this worktree first on `PYTHONPATH` to avoid editable/import leakage.
- Next: delegate non-overlapping location implementation and probability feasibility; define source-strength sizing using current consumer contracts, then tests, review, and explicit beta integration gate.
- Implementation checkpoint: none yet (base is not an implementation receipt).
