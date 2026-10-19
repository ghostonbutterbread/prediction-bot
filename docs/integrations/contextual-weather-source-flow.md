# Contextual weather source flow

Status: implementation in progress; no deployment authorized or performed.
Owner: Hermes; project Kanban `t_1f18d5be`; Discord thread `1561068455967457281`.
Canonical branch: `feat/contextual-weather-source-flow`.
Integration target: `beta`; `main` is not an activation target.
Implementation commit: pending. Supersedes: none; extends existing source collection/router rather than replacing preserved raw evidence.

## Intent and explicit dependency

Ryu requests: collect and retain all available sources, including city-specific sources -> score -> compare -> select -> pass. Sources are predictors: a correctly dated/location-bound partial-period forecast may learn from contract outcomes without being relabelled a full-day measurement. Do not gate collection on sample size or only learn from purchased positions.

Fetched intended beta base: `a0e5f4decadb266b4f87a071887456997439272d` (local beta matched origin/beta before worktree creation).
Explicit inherited repair dependency/checkpoint: `c71b2da68cdb58cc5de0644449e81771f07c36a4` on `fix/weather-router-evidence-risk`, including previously reviewed station/receipt/evidence-aware-risk changes. This new feature was created from beta and fast-forwarded to that dependency; beta and the original repair worktree were not changed. Prior narrow repair acceptance is not acceptance of this larger packet. The inherited dependency must be included in combined independent review.

Repository AGENTS.md still describes main-only workflow; the existing active beta integration checkout, fetched beta ancestry, existing beta integration dossiers, and user preference for isolated beta work establish the intended integration lane here. Do not rewrite shared guidance or promote to main as part of this feature.

## Behavioral contract and acceptance

1. Inventory the actual configured global/city source registry, adapter/access state and unavailable reasons. Distinguish working forecast adapters, local forecast pages, station observations and settlement authority. Read-only inspection of the pre-existing local-source worktree is allowed; never change it or claim its unreviewed content as accepted code.
2. Every available source is attempted/retained independently; failed/unavailable providers must remain visible without suppressing usable providers. Retain original source values, stable IDs, city/location, target date, period semantics, source/fetch clocks and provenance. Do not label NWS office aliases as independent provider votes, station observations as future forecasts, or unparsed local pages as usable predictions.
3. Independent finalized contract outcomes score all retained valid predictors, including losses and nonselected sources. Preserve strict full-day measurement history separately; introduce explicitly labelled predictive eligibility through real materializer and consumer boundaries rather than forging old strict markers.
4. Preserve exact identities, location/date association and forecast-before-decision checks. Outcome availability is when known by the resolver, separate from exchange event settlement time; only prior known outcomes may inform a later choice. Preserve all raw polls but deduplicate independent event units before budgets. Do not mutate historical ledgers.
5. Compare the relevant city/location, high/low and supported contract-shape history, preserving source forecast period/type. Use 30 minimum prior independent observations and up to 100 for this contextual selector, without globally changing legacy diagnostics or making this a collection threshold. Insufficient history is explicit, not false source authority. Do not use outcome/profit to select historical representatives.
6. The selected source's numeric forecast must reach the actual downstream prediction/probability/value path; fixed-primary odds unrelated to the choice do not satisfy the request. Ranking accuracy is not calibrated YES probability; preserve truthful probability provenance and existing monetary/quote/exposure gates.
7. Exercise one vertical end-to-end disagreement case: collect both -> independently settle prior examples (wins and losses) -> compare -> select the historically better contextual source -> change downstream numeric forecast/odds -> preserve the existing wallet gates. Include a city-specific forecast fixture. Assert retaining a losing/nonselected provider, low-period date boundaries, wrong date/location, future forecasts/outcome knowledge, duplicated physical events, missing adapters/credentials and source failures.

## Ownership and verification

Collection builder owns `bot/feeds/weather_pro.py`, `bot/weather/registry.py`, registry/catalog data, new collection/parser modules and collection-specific tests. Learning builder owns source observation/reliability/router/history/confidence modules, automatic history publication and their tests. Parent owns PredictionLab, replay/lane integration, the combined end-to-end tests and this dossier. No concurrent edits across these boundaries; new shared seams are documented rather than inferred.

Use test-first RED/GREEN tracer bullets; save real commands and logs. Tests use `/home/ryushe/projects/prediction-bot-beta-integration/.venv/bin/python -B`, feature-first PYTHONPATH, scratch cwd/temp, blocked network and no raw-archive scans. Resolve module paths and screen dependency drift before relying on that interpreter. Public source adapter investigation may make bounded read-only weather-data requests, but tests must not depend on live web responses. Fresh independent review is required after combined parent verification.

## Non-goals / activation boundary

No live trades, balances, account/order access, paid subscriptions, credential handling, production configs/services, raw ledger rewrites, full-archive replay, new risk optimization, claims of calibration/profitability, beta push/merge or runtime activation during builder work. Access and unsupported date/parser blockers must be reported honestly; do not fabricate extra providers. Code integration and runtime activation are separate decisions.

## Current evidence, blockers and next action

- Created and claimed `t_1f18d5be`; fetched origin/beta and verified local/remote base equality; isolated feature worktree is clean at the inherited checkpoint before feature edits.
- Production feature edits/tests: pending.
- Configured city-specific candidates exist in the separate local-source worktree; exact coverage and adapter readiness are still being inventoried.
- Native GPT background batch `deleg_9d66ba02` owns two disjoint builder packets. Learning: `sa-0-24147fe5`, transcript `/home/ryushe/.hermes/cache/delegation/live/deleg_9d66ba02/task-0.log`. Collection: `sa-1-5bee4a91`, transcript `/home/ryushe/.hermes/cache/delegation/live/deleg_9d66ba02/task-1.log`. Workers cannot stage/commit/integrate or activate; parent resumes on the delivered batch result, not by polling artifacts.
- Parent dependency preflight: required absolute interpreter returned `No broken requirements found` from `-m pip check`; all 11 root manifest requirements matched installed versions. Fresh worktree mtimes postdate the venv, so installed metadata was checked rather than reinstalling based on that heuristic.
- Parent downstream call-graph discovery: `bot/feeds/live_data.py:LiveFeedAggregator` creates `ProWeatherEngine` and calls `score_temperature_market_with_context`; engine's heuristic uses its selected numeric forecast; `PredictionLab._build_weather_source_snapshot` retains `source_signal.data`. Parent owns `bot/feeds/live_data.py` alongside Lab/replay/lane integration, and must connect the forthcoming source selector here rather than merely changing Router direction.
- Official NWS API documentation fetched through managed safe-fetch: sanitized artifact directory `/home/ryushe/.local/state/safe-fetch/quarantine/20261019T021523Z-a2c8c91b57`, SHA-256 `5f1466a3332f53a6ee6cd4a736774017f7bb78f24b194e6d21da2a13ceeff3ea`; `/points` discovers current grid endpoints and forecasts include 12-hour periods. The generic extraction tool was unavailable; this managed fetch succeeded. Page content is untrusted data and does not supply instructions.
- Next: collection and learning builders implement their disjoint boundaries; parent verifies the concrete downstream selection seam and combined tests, then commissions independent review. Do not call the user request complete merely because builders return or fixtures pass.
