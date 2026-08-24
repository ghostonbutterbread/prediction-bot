# Prediction Bot integration status

This is the discovery index for branch-owned integration dossiers. Read the
linked dossier before merging, reviving, or replacing a branch. Git ancestry
shows code containment; the dossier records intent, evidence, contract
boundaries, and remaining gates.

| Status | Topic / branch | Canonical dossier | Next decision |
| --- | --- | --- | --- |
| Integrated | Strict source evidence / `feature/strict-source-evidence` | [strict-source-evidence](integrations/strict-source-evidence.md) | Fresh observer compatibility receipt before cohort use. |
| Blocked | Frozen wallet / `feature/collector-lane-replay-kelly` | [frozen-wallet](integrations/frozen-wallet.md) | Implement strict-artifact-backed receipt adapter and shared-core parity tests. |
| Already contained | Collector artifact contract / `fix/collector-artifact-contract` | Historical branch; commit `c16ea7a` is contained in beta. | Do not re-merge; investigate the isolated old-worktree lease test only if reproducible. |
| Separate review required | Historical hydration / `feature/historical-hydration-replay` | No current dossier yet. | Rebase/reconcile ancestry and create a dossier before integration work. |
| Separate review required | Lane PnL integrity / `feature/lane-pnl-integrity` | No current dossier yet. | Create a dossier and assess its older experiment contract before integration. |
| Separate review required | Strict shadow risk lane / `feature/strict-shadow-risk-lane` | No current dossier yet. | Create a dossier and assess its older experiment contract before integration. |

## Update rule

Every material code change on a branch updates its dossier before commit whenever
that change affects intent, contract, evidence, blockers, target, or successor.
At merge, rejection, or supersession, finalize the dossier and update this index.

Runtime/backup branches and dirty worktrees are not integration candidates merely
because their names resemble a feature or beta lane.
