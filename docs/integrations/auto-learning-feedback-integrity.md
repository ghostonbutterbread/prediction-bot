# Feedback identity and SQLite cleanup — integration dossier

- **Status:** awaiting parent independent review; not integrated or activated
- **Owner / branch:** `fix/auto-learning-feedback-integrity-t_2f4bb125`
- **Base:** `3d3321fe5a6301c07409dac08ed55bfc9091851f`
- **Intended integration target:** `feat/auto-learning-loop-t_2f4bb125` (parent owns review and merge)
- **Updated:** 2026-11-20
- **Implementation / immutable recovery checkpoint:** `a167fb373da9f011d60360970877a0b444732887`; the later commit containing this dossier is handoff-only.
- **Inspiration:** integrated review `deleg_fa5a420e` (duplicate experiment feedback and unclosed connections).

## Implemented contract and boundaries

Feedback rejects equal baseline/candidate experiment IDs before output publication, including across different stores. Existing distinct-ID behavior is retained. Both feedback reads and auto-research compare, queue and run close connections after SQLite's existing transaction context exits; `database()` also closes on initialization failure. No SQLite URI, runtime, journal or storage settings were changed. Commit and rollback were checked against actual disposable SQLite databases. Nothing was deployed, scheduled, merged, pushed, or run against collector/paper data. Sibling `feat/auto-learning-strategy-inputs-t_2f4bb125` also changes `scripts/auto_research.py` and its tests; parent must reconcile and test the combined tree.

## RED / GREEN evidence

Working directory: this branch's worktree, with `PYTHONPATH="$PWD"`, `PYTHONDONTWRITEBYTECODE=1`, and isolated `HOME`, `TMPDIR`, `XDG_CONFIG_HOME`, and `PREDICTION_BOT_COLLECTOR_ROOT` under a new `mktemp -d /home/ryushe/.hermes/cache/scratch/feedback-tests.XXXXXX` directory. Tests use disposable synthetic stores and fixtures. The following selections are exact; no wallet, future-source-clock, native-cron Docker, or other blocked probes were run.

- **RED identity:** `python3 -m unittest -v tests.test_research_feedback.ResearchFeedbackTest.test_identical_experiment_ids_are_rejected_without_output tests.test_research_feedback.ResearchFeedbackTest.test_identical_experiment_ids_in_separate_stores_are_rejected` — 2 failures, both CLI return codes unexpectedly 0; after fix, same tests plus `tests.test_research_feedback.ResearchFeedbackTest.test_separate_stores_remap_and_same_store_compatibility` — 3 passed.
- **RED feedback cleanup:** `python3 -m unittest -v tests.test_research_feedback.ResearchFeedbackTest.test_feedback_closes_connections_on_success_and_load_failure` — failed because a connection remained open. GREEN: same selection passed, including injected load exception and no output.
- **RED auto cleanup:** `python3 -m unittest -v tests.test_auto_research_connection_lifecycle` — 4 failures, each a missing close in compare, schema initialization, queue or run. GREEN: same 4 passed; SQL failures injected after insert prove rollback and successful paths prove commit. Comparison includes successful full in-process comparison in the feedback tests as well as failed-query paths.
- **Final committed implementation GREEN:** `python3 -m unittest -q tests.test_auto_research_connection_lifecycle tests.test_auto_research tests.test_research_feedback` — 27 passed (4 + 2 + 21); includes existing CLI replay/queue/comparison and separate-store feedback with real synthetic producer. `git diff --check` and staged diff check passed. Existing test fixtures use SQLite connection context managers without closing, producing `ResourceWarning`s during the full selection; production closures are independently asserted in-process. Full suite was not run because only the named safe synthetic scope was authorized.
- **Harness caveat:** The first identity RED command incorrectly formed its scratch path from inherited `HOME`; `mktemp` failed and the shell continued, so two disposable `TemporaryDirectory` fixtures fell back to `/tmp` before cleanup. That receipt was discarded. The RED was repeated using the absolute scratch path and `set -eu`, as were subsequent runs. No persistent input or service was targeted. A commit attempt under disposable `HOME` lacked Git identity; retry used the previous commit's explicit author identity without modifying Git config.

## Remaining assumption and gates

- SQLite `mode=ro` on WAL databases can create sidecars; the caller must continue to provide quiescent stores as already documented. This change neither tests nor changes SQLite journal/storage policy, and makes no general immutable-read claim.
- Parent must obtain independent review of this exact range, reconcile sibling overlap and run relevant synthetic tests on the combined integration tree before merging. Remove this branch-local dossier from the integration target upon acceptance. Runtime activation and stable promotion remain separate decisions.
- **Resume point:** review implementation commit and dossier; integrate only through the parent-owned branch after the above gates. No task-owned uncommitted changes are intended at handoff.
