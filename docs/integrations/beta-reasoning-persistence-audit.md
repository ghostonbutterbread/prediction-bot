# Beta reasoning, data-flow and persistence audit

- **Status:** implementation and reproduction
- **Owner:** Hermes; project Kanban `t_500d82fb`; Discord thread `1546659523605962934`
- **Branch:** `fix/beta-audit-persistence`
- **Worktree:** `/home/ryushe/worktrees/prediction-bot-beta-audit`
- **Base commit:** `4ca50c30c39707fd13e4b657d6299667cd761580` (`main`, lowest shared owner of configuration and accounting behavior)
- **Audited beta:** `cf234f98598eab0e2e31d5826eae3d7e5ca6fe5f`
- **Intended integration target:** `beta`, never `main` or an active runtime
- **Last updated:** 2026-09-07
- **References:** user audit request; repository AGENTS.md; existing untracked beta `docs/DATA_REORGANIZATION_HANDOFF.md` (read-only, separate ownership)

## Intent and success criteria

Reproduce and minimally fix concrete reasoning/data-flow defects. Verify that a selected beta cohort's collector, shared snapshot, paper consumers, resolver and derived source history use one persistent storage location across code-checkout/CWD changes. Run focused regressions, an artifact-linked deterministic E2E fixture, the isolated complete suite, and an independent review. Report evidence gaps instead of claiming all strategies are profitable or formally correct.

## Boundaries

Paper/observer/synthetic fixtures only. No live orders, balance or real-wallet access, raw historical edits, migration, deletion, service restart, schedule changes, live configuration edits or push. No integration of separate composed-wallet feature merely to increase audit coverage. The active collector executes code from the beta integration worktree, so changing beta itself is operationally sensitive even without restarting its process. Beta also contains an unrelated untracked migration handoff. Keep it untouched; integration requires explicit reconciliation/activation decision.

## Reproduced evidence

The same active external cohort config loaded from two CWDs resolves `runtime.base_dir`, `data_dir`, `shared_market.runtime_root`, and resolver outputs beneath each CWD, while its compact replay index and raw resolver input remain absolute beneath `/mnt/data-collection/prediction-bot`. This splits producer/consumer identity and state when switching checkouts. Active services currently rely on the mounted checkout as WorkingDirectory.

Active collector and resolver are writing current artifacts; only the collector is a continuous process. Resolver uses a timer. Source Router promotion has failed historical service state and no active timer; paper consumer is inactive. These are operational states, not strategy correctness findings.

## Implementation plan

1. Preserve legacy non-beta semantics unless a storage root is explicitly selected; beta composed profiles select the canonical collector root. Resolve only owned data fields, not source code/config/definition paths or provenance.
2. Minimal regression-backed fixes for verified decision/settlement defects, if found.
3. Connect real internal collector/index/resolver/materializer/router functions using fake external market/weather boundaries and temporary output roots.
4. Commit shared ancestor fixes once, merge audited beta into this task branch, and rerun tests. Leave target/runtime unchanged unless safe integration is separately approved.

## Evidence and review

- Baseline isolated suite: **1,044 tests, 3 errors, 7 skips**, all errors from three composition-sweep tests assuming ignored `data/summaries` existed. Log and audit repros preserved in `/mnt/data-collection/prediction-bot/data/derived_reports/beta_audit_20260907/`.
- Composition persistence regressions: RED reproduced checkout-default outputs, rejection of collector-root output, and missing downstream discovery; GREEN **5 new tests**, **15 existing composition tests**, **4 unified-corpus tests**. Old sweep tests now allocate their own temporary collector root, not an ignored repository directory.
- Parent independently reran the auditors' accounting and settlement bad-behavior reproducers successfully; desired data-contract regressions failed as expected. Dedicated non-overlapping builders are fixing actual paper lifecycle, source-evidence qualification/chronology, and snapshot/settlement identity handoffs.
- Fresh active collector sample: 20 complete rows from a bounded 2MiB tail; export accepted 20/rejected 0. 80 source rows: 40 strict-complete forecast candidates, 20 forecast-unavailable, 20 observation-only. This is bounded field compatibility, not full-cohort performance evidence.
- No final full-suite, review acceptance, or runtime activation claim yet.

## Blockers and deferred work

- **Integration:** beta is an active code source and has an unrelated untracked handoff. User decision required before changing that worktree; fixture verification can complete independently.
- **Live operational E2E:** paper consumer and promotion are disabled. Requires explicit authorized activation into a named fresh paper cohort after retention/capacity preflight. This audit will not restart or enable them.
- **Tracking:** parent Kanban `claim/show` was incorrectly denied as a delegated-child context after dispatch. Logged sanitized papercut; retry after child completion. Card creation succeeded.
- **Historical replay:** no full multi-GB replay or strategy profitability claim in this task. The composed-wallet feature remains separately owned/unmerged.

## Decision record

- 2026-09-07 — created isolated worktree from owning shared ancestor; inspected beta/runtime authority; reproduced CWD-dependent split; began bounded audit.
