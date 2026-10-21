# Weather capture clock ownership — accepted beta handoff

- Owner/task: default / prediction-bot `t_d4051542`.
- Authorization: Ryu Discord message `1562326276495249440`, fix the identified defect and promote if good.
- Owning branch: `fix/weather-capture-clock-ownership`.
- Worktree: `/home/ryushe/worktrees/prediction-bot-weather-clock-repair`.
- Blocked input: `4b7ee3e4fadc4855faa4564dabb8ad64ecbf82a6`.
- Repair implementation: `baffe8c` (reachable ancestor of the owning branch).
- Combined code checkpoint: `6f2b4ec64c7161025eb9541cc72645465382c598`.
- Independently accepted code tree: `9f5295f7c39a2a0365962c4bf9c02eaf232d0629`.
- Integration base/target: `a0e5f4decadb266b4f87a071887456997439272d` -> owned `beta`; never `main`.

## Contract and evidence

Compare provider issue times to their own/enclosing captures; compare aggregate completion aliases to the aggregate capture, not an earlier provider. Retain original aliases and reject invalid/naive/future/conflicting clocks and real issue-after-capture violations. Permanent actual producer -> Lab -> sealed replay -> independent resolver -> materializer test was observed RED on the blocked input and GREEN after repair, with frozen-clock control and negative controls.

Combined candidate includes original collector-only defaults at `9f13ec9997f94f5791e1f7c605ac9646196bba34`; original feature worktree remains unchanged. Defaults apply only to observer collectors, preserve explicit opt-out/history settings, and do not enable trading/wallets.

Independent reviewer APPROVE: `independent-review.md` / `independent-review.json`, zero blocking/security/logic findings. Parent gates: clock3, functions19, focused290, full1759 with seven absent ignored-runtime-config skip entries and one exact production-archive smoke excluded; old reviewer clock probe2; producer-free readback6/120files; publication fault/retry12; real filesystem/retry4. Independent reviewer reran 1737 discovery methods, four custom methods and producer-free readback; test sets overlap, not additive.

Durable evidence root: `/mnt/data-collection/prediction-bot/data/derived_reports/weather_clock_repair_1562326276495249440/`. Includes exact commands, original RED, GREEN receipts, reviewer scripts/logs, candidate source archive, synthetic artifacts, Git-blob verification, readiness report and checksums. Scratch is not the sole evidence source.

## Final integration and activation boundary

This tip adds only this branch-local handoff after the accepted code checkpoint. Exclude it from the beta merge and require `git write-tree` to equal the accepted tree above before committing. Then run post-merge tests from an exact Git export of beta, push only from the clean beta integration worktree, and read back the remote ref/tree. Record the final beta commit in the external report and Kanban; retire only this clean contained repair worktree/branch.

No service restart, runtime configuration/scheduler change, raw-history rewrite, wallet/order operation or trading enablement is authorized by code acceptance. Indexed SourceWriter is already integrated; bounded predictive publication after the single existing resolver owner, capture/history/temp-space budgets, controlled observer rollout and fresh forward resolution -> publication -> later-consumption evidence remain separate operational work. The current full-archive publisher must not be enabled against the large archive. See durable `READINESS.md` for the concrete boundaries.
