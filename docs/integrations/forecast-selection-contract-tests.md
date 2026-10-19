# Forecast-selection test phase — t_860b3b33

Status: intentionally RED; test-only handoff to implementation t_6c73f5cc.
Owner/ref: local test/forecast-selection-t_860b3b33 in the task's isolated clone.
Parent feature: feat/contextual-weather-source-flow @ 1bd33fe11e20388fb5b72ea5e72d16bc6157cd25.
Repair-only RED baseline: c71b2da68cdb58cc5de0644449e81771f07c36a4.
Intended eventual integration target: beta, only after the existing feature's independent release gates. This test card neither integrates nor activates anything.

## Contract and scope

Adds only tests/test_forecast_selection_contract.py. Synthetic fixtures use the actual binder, materializer, selector, weather engine, aggregator and PredictionLab snapshot seam. No existing test helper imports or public capture files. The baseline compatibility shim supplies only supported constructor/materializer keywords, not missing behavior.

The source inventory sibling's weather_source_inventory_and_evaluation_contract.md, sections D.3–D.4, requires observer-only abstention when sources have indistinguishable contextual support; ID ordering may make evidence deterministic but cannot establish superiority. The new tie test deliberately enforces that proposed evaluation contract. The implementation owner must reconcile this tie policy explicitly if changing the contract rather than silently treating the pre-existing lexicographic route as acceptance.

## Executed evidence

Same test bytes on both snapshots, SHA256 aeb1f97e6e96f7d7b111ff71b7c1948e58689a61f3315ebd2c0e2a9d770dd9c2.

Command, from each snapshot:

    /home/ryushe/projects/prediction-bot-beta-integration/.venv/bin/python -B -m unittest discover -s tests -p test_forecast_selection_contract.py -v

Repair baseline: 13 tests, 11 assertion failures, 0 errors, 2 passing controls; 444 tracked files unchanged.
Current feature: 13 tests, 1 assertion failure, 0 errors; 12 pass; 467 tracked files unchanged.
Remaining RED: test_equal_contextual_support_abstains_instead_of_claiming_id_order_superiority. Two sources each have 30 independent events, 25 wins/5 losses. Selector returns routeable=True through its source-ID tiebreak instead of abstaining.

Neighbor commands use the same interpreter/unittest discovery with patterns test_weather_forecast_location.py, test_weather_location_replay.py and test_weather_probability_evidence.py: respectively 17, 5 and 7 tests pass, zero failures/errors. No full-suite acceptance is claimed.

Receipts/logs are in the task handoff's evidence/comparison.json, evidence/regressions.json and corresponding logs/manifests. The task attachment report carries absolute paths and recovery instructions. run_contract.py returns success only when the expected RED is reproduced and the neighboring checks pass; it is not a green feature gate.

## Safety / deferred gates / exact resume

No production edits, raw rewrites, archive replay, secrets, network activation, operational actions or service/config changes. New fixture raw inputs are byte-compared after each test; sockets are mocked/denied in the new suite. This is not a kernel sandbox or the pending retained-store verification.

The original feature dossier's CAP final-readback/operational-retry checks and 19 withdrawn function tests remain unapproved and unexecuted. None was recreated or used as an equivalent through this task. Its existing feature-integration block remains intact.

Next: implementation t_6c73f5cc imports this test-only patch onto the existing feature, resolves or explicitly reconciles equal-support tie abstention, and runs the new suite directly (must become green, not the expected-RED receipt script). Review/release belongs to the already-created downstream cards. Do not reimplement the already passing collection/history/location/receipt/risk paths.
