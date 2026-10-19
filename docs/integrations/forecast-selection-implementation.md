# Forecast selection implementation continuation

Status: implementation gate GREEN; independent combined review and all original release permissions still required. This is not approval to integrate into beta or activate anything.
Owner/task: Hermes / t_6c73f5cc. Review owner: pre-created t_6c52f822; release owner: t_d0fb16e8.
Branch: fix/contextual-tie-abstention-t_6c73f5cc.
Checkout: /home/ryushe/.hermes/kanban/boards/prediction-bot/workspaces/t_1f18d5be/t_6c73f5cc/candidate.
Continuation base: e54114a6afbff37fbd7bd6eb2af1e6859199e2b3 (test dossier), with test implementation 3a3ae0550c0633b6d11cb3c3847697d10f8f6f69, atop canonical feature 1bd33fe11e20388fb5b72ea5e72d16bc6157cd25. No feature reconstruction or replacement of upstream collection/history is necessary.
Intended eventual integration target: beta, after independent acceptance and explicit disposition of outstanding permission gates; main is not a target. No beta fetch/preflight/integration or canonical feature mutation was needed for this pinned local continuation. The local source/test ref was fetched before editing.

## Intent and prerequisites

Implement inventory evaluation contract D.3-D.4: stable source-ID ordering is not empirical superiority. Reuse the twelve already-passing collection, independent settlement, contextual history and downstream behavior tests. No source adapter, history algorithm, odds model, risk limit or configuration is rebuilt.

Required inherited repair: c71b2da68cdb58cc5de0644449e81771f07c36a4. `git merge-base --is-ancestor` returns zero on this continuation, before edits and in the verification runner. It carries the reviewed station/location, receipt and evidence-aware-risk packet described in weather-router-evidence-risk.md. The inventory prerequisite t_ab4530f1 records verification of that exact packet. Completed repair card t_cb69b05b is additionally linked as an explicit Kanban parent of t_6c73f5cc; readback shows no unsatisfied parents. No broader unfinished probability/calibration or separate wallet-exposure branch was copied or declared accepted.

The inherited repair and this feature remain subject to fresh combined review. Narrow ancestor acceptance is not global release approval.

## Implemented contract

Only bot/weather/source_router.py changes production behavior. Its support key preserves existing ordering by eligibility threshold, prior directional accuracy, prior average realized binary edge when present, and independent sample count. Source ID remains a stable display/order tiebreak, not part of empirical support.

For predictive mode only, multiple eligible sources sharing the top support key now return routeable=false, null chosen identity/name, and blocker unresolved_contextual_source_tie. The direct selector exposes sorted tied_source_ids for audit. The existing compact downstream receipt retains the blocker and all source scorecards rather than the direct selector's full observation objects. The wrapper consequently returns no chosen numeric forecast. A unique top score still routes; lower-ranked ties do not block it. Legacy strict diagnostic mode and its source-ID ordering are unchanged.

All current-source/date/location/clock validation, independent outcome binding, known_after < decision-time history, event deduplication, default 30 minimum/100 maximum, immutable raw provenance, strict-versus-predictive separation and probability provenance remain inherited and exercised. There is no rank-to-probability conversion and no new claim of statistical significance or calibrated superiority. A one-event advantage means uniquely highest under the existing ranking policy, not proof of true forecasting skill.

The aggregator retains the preexisting raw heuristic as diagnostic evidence during abstention. Both actual EnhancedStrategyEngine APIs reject the nonrouteable receipt before it or a price/news fallback becomes an actionable decision. Removing diagnostic odds was not the existing receipt contract and was not added here.

## Reproducible evidence

Evidence directory: checkout parent t_6c73f5cc.
Required interpreter: /home/ryushe/projects/prediction-bot-beta-integration/.venv/bin/python.

1. Before changes, direct unittest discovery for test_forecast_selection_contract.py: 13 run, 1 assertion failure, 0 errors, 12 pass (contract-red.log). Failure is equal-support routing by ID.
2. After the production fix, the unchanged original 13 tests pass (contract-green.log).
3. Added three permanent contract cases: actual aggregator and both strategy APIs abstain on a tie with a tempting price fallback; one-event advantage still routes; top ties versus lower ties among three sources behave distinctly. Original 13 tests are retained unchanged.
4. Final command: required interpreter -B ../verify_candidate.py (or its absolute path from the evidence directory). Real final result: 210 unique tests, 0 failures/errors/skips, 28.356 seconds. Fifteen explicitly selected modules include all 16 contract tests, contextual histories and source flow, location/replay/probability repairs, strict Router, materializer, local low/TEGNA pipelines, configured registry, FOX/TEGNA/OTHER parsers. verification.json lists every module/count and test ID; implementation-gate-final.log is the complete output.
5. Registry coverage reuses existing configured tests rather than inventing extra sources: 24 local candidates, 20 adapter-capable and 4 named capture-only limitations, with trust/sample counts unchanged; FOX/TEGNA high-low dispatch uses no test adapter override. Existing parser/pipeline tests preserve WLS-low restrictions and partial-low semantics.
6. Runner verifies all 11 manifest requirements against installed metadata and all three core module paths beneath this checkout, requires nonzero module counts and unique IDs, and compares 469 tracked-file hashes before/after execution. Synthetic raw inputs/resolutions are byte-checked by the contract fixture. Network guarded by Python socket mocks and MockTransport, not kernel confinement. No full archive discovery/replay is invoked.
7. This is a targeted implementation gate, not a full repository or release suite. Previous 1,710-test evidence belongs to its original pinned feature, not this new tip. Exploratory extended runs exposed incorrect assumptions about compact audit keys and removal of diagnostic odds; the test oracle was corrected to the actual downstream contract and the strategy gate then exercised. Those runs are not counted as product RED/GREEN proof; logs remain preserved.

## Approval boundaries and successor

The same CAP final separate-process retained-store audit and operational storage-failure clean-retry remain unexecuted and require explicit operator disposition. Their exact commands/fixtures remain in the original contextual-weather-source-flow.md dossier (contextual-cap-fresh-review-h9ioz73k report). Do not recreate equivalents through a different runner/worker. The separately withdrawn 19 function-style regressions remain unselected/unimported/unexecuted; existing FOX's 19 unittest methods are distinct.

No secrets, subscriptions, balances, orders, accounts, collectors, services, timers or live runtime configurations were accessed or changed. Feature selection remains opt-in/disabled by default. No raw historical rewrite, full archive replay, merge, push, deployment or activation occurred. Canonical feature remains clean at 1bd33fe.

Next: recover this exact continuation and attached evidence; independently review the complete candidate, especially tie support semantics, strict-mode compatibility and no-fallback behavior. t_6c52f822 must issue a fresh independent verdict and preserve all prior approval gates; t_d0fb16e8 must not integrate or activate while any required gate remains open. Implementation completion only releases that pre-created review dependency.
