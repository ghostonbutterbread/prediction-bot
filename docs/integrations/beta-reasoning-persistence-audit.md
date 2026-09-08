# Beta reasoning, data-flow and persistence audit

## Ownership and decision
- Owner: Hermes; prediction-bot Kanban `t_500d82fb`.
- Branch/worktree: `fix/beta-audit-persistence`, `/home/ryushe/worktrees/prediction-bot-beta-audit`.
- Shared ancestor: `4ca50c30c39707fd13e4b657d6299667cd761580`.
- Audited/local target beta: `cf234f98598eab0e2e31d5826eae3d7e5ca6fe5f`.
- Implementation checkpoints: `41f75408eef2b5f2c8a57d67e9841c6835b94261` (composition paths), `6ae5fc24282fc4c0b172b47ce55437338cf689b7` (accounting/evidence/storage fixes). This merge checkpoint adds beta maintenance wiring and contains the reviewed complete integration.
- Status: independently approved for local beta integration; never main.
- User approved synthetic persistence checks and local beta integration. No push, restart, live trading, raw-history edits, or lane activation.
- Latest explicit user decision supersedes beta's arbitrary-output-directory change: reports remain under selected storage root; consumers validate shape/eligibility. Preserve unrelated untracked beta migration handoff byte-for-byte.

## Implemented contracts
Persistent selected storage across CWD/worktree changes; env > configured > default root authority; report containment; signal audit persistence; composition output discovery. Shared Kelly env/config resolution, consistent entry fees through settlement/reload, VOID reservation release without economic attribution, NO-side mark-to-market, explicit settlement chronology. Immutable candidate cutoff/snapshot identity, exact settlement joins, authoritative receipt timestamps. Forecast vote/target-period proof, capture-before-settlement strict history, ISO-date parsing correctness. V2 derived generations and runtime rejection of old incompatible history; immutable old artifacts retained. Maintenance passes configured storage authority to the actual promotion consumer.

## Verification
- Final combined beta tree: **1,109 tests OK, 7 skipped**; `/mnt/data-collection/prediction-bot/data/derived_reports/beta_audit_20260907/final-beta-integration-suite.log`.
- Shared ancestor fixes: 1,102 tests OK, 7 skipped, `/tmp/predbot-final-ancestor-suite.log`.
- Actual disposable Git switches fixed 6ae5fc2 -> old beta cf234f9 -> fixed 6ae5fc2: same raw/state paths, checkpoints and row counts 1/2/3, first-row hash unchanged. Receipt `branch-portability-6ae5fc2.json` in audit report directory. External absolute paths maintain old-code compatibility.
- Actual PredictionLab with config-only root and relative cohort, fresh interpreters CWD A -> B -> A: same paths, checkpoints/rows 1/2/3, first-row hash unchanged, no checkout files. Receipt `config-only-cwd-persistence.json` in audit report directory.
- Real collector-to-router E2E, paper lifecycle regressions, and config-load -> maintenance -> real publication covered in suite; external boundaries are synthetic and no live orders occur.
- Independent approvals: residual Kelly/storage `deleg_8f2515b9`; runtime history `deleg_e3739fcb`; final integration `deleg_2b2414a8` (70 focused tests). Frozen V1 fixture has pinned SHA-256 and provenance to41f7540; differs from ancestor only by terminal newline, AST identical; no runtime Git dependency.
- `git diff --check` clean. Final working-tree hunks explicitly staged before commit.

## Integration and residual boundaries
Local beta is the authorized target; remote `origin/beta` does not exist (fetch attempted and returned missing ref). No remote branch is created or pushed. Beta checkout is an active source, but no process restart or configuration/timer/lane activation is part of this task. Maintenance remains disabled by default. Old V1 history requires explicit derived re-materialization before runtime use; this task never rewrites historical evidence.

A missing manifest may raise FileNotFoundError instead of a structured SKIP (inherited fail-closed behavior); no claim that every I/O error becomes structured verification failure. Seven suite skips remain reported, not counted as executed coverage. Historical profitability, full multi-GB replay, and live operational paper-cohort activation are outside this synthetic correctness audit.

After local integration, remove this temporary dossier from beta; retain durable contract in `docs/architecture/persistent-storage-contract.md` and test receipts in the audit report directory. Record final beta commit and post-integration verification on Kanban. No unrelated worktree cleanup.
