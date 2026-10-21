# Contextual direct dataset consumer — task t_608d5235

Status: isolated candidate; independent review pending.
Branch: `fix/contextual-direct-dataset-t_608d5235` at fetched `origin/beta` `7d72c9d74e7dace5f498d85deda33906f961beb1`.
Target: `beta` only after independent exact-tree review. No runtime activation, config/service changes, trading, archive replay, SourceWriter, source-copy or publisher.

Intent: adapt the contextual weather consumer to the existing indexed collector evidence and independent resolutions. The strict SourceRouter result is a full-day measurement scorecard and cannot substitute for predictive partial-period rows. Explicit direct paths override the old materialized-history metadata input; the latter remains compatible when direct paths are absent. Qualification uses shared sanitizer, indexed receipt/hydration, independent resolver binding, ledger clock/proof predicates and independent event identity; the existing predictive selector retains 30/100, tie abstention and numeric forecast/risk boundaries.

Evidence: New RED initially failed importing the nonexistent direct predictive loader. After implementation, `test_contextual_direct_dataset.py` 5/5 and actual contextual engine-to-wallet direct-index test 1/1 passed; existing contextual compatibility module 15/15 passed. Full offline suite from repository cwd passed 1,759 methods, 0 failures/errors, 7 absent-config skips; exactly one production-archive smoke was excluded (`test_replay_decision_input.ReplayDecisionInputTests.test_real_archive_first_row_builds_in_default_legacy_compatibility_mode`). First runner attempt from parent cwd yielded three path-relative fixture errors, resolved solely by changing runner cwd; successful rerun is `../offline_suite.py` with receipt `../offline_suite.log`. Bounded synthetic rows under disposable scratch only; no production dataset read. Independent exact-tree review pending.

Activation boundary: code availability is not effective runtime configuration or observed live learning. The collector's compatibility metadata default remains unchanged; owner of resource/config/collector rollout is root t_1f18d5be. The operator confirmed no active qualification owner overlaps this direct-consumer adaptation; old t_9cd570ba integrated direct strict reader 6969006, generated-manifest branch is not prerequisite.

Next: run complete offline suite excluding production archive probe, verify reference impact and exact diff, commit feature; obtain fresh independent review before any beta promotion. No push/merge authorized by this task without review.
