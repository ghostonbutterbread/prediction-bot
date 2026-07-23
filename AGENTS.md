# AGENTS.md — Prediction Bot Engineering Guide

This repository operates paper-only prediction-market research and forward-shadow evaluation. Treat `main` as the reviewed shared framework; keep experiments explicit, reversible, and evidence-driven.

## Non-negotiable safety

- **Paper / simulation / observer only.** Never place live orders, mutate wallets, access balances, or enable live trading unless the user explicitly authorizes it for the exact task.
- A Git merge is **not** a deployment. Do not restart services, alter ignored runtime configs, or enable a lane merely because code has merged.
- Preserve the operational worktree at `/home/ryushe/projects/prediction-bot`. It can contain intentional runtime-local changes. Do not reset, clean, overwrite, or merge into it casually.
- Do not modify historical raw ledgers. Derived reports and replay outputs must be separate, reproducible, and clearly labeled.

## Canonical worktrees and Git workflow

- Use a clean worktree based on `main` for implementation and review. Do not use the dirty operational worktree as a merge target.
- `main` contains reviewed shared infrastructure, controls, integrity fixes, and accepted paper-shadow lanes.
- An experiment branch contains **one coherent behavioral hypothesis**. Create it from current `main`.
- Keep at most one active child experiment per hypothesis area. Do not make a new branch only to rerun a replay or change comparison settings.
- Before a merge, verify actual ancestry with `git merge-base`, run the appropriate tests, inspect `git diff --check`, and preserve a linear history when possible.
- Merge an experiment into `main` only after its code/test review and its explicitly defined evidence gate are satisfied. Otherwise retain it paper-only or delete/archive it.

### Current lane pattern

```text
main
  └─ feature/source-router-ev-shadow
       └─ guarded source-router comparator
```

`feature/source-router-ev-shadow` is the isolated code boundary for the disabled `shadow_source_router_no_price_guard` experiment. It inherits `main`; it is not a deployment branch and must not be enabled by default.

## How to run a lane experiment

1. Keep the existing lane unchanged as the **control**.
2. Put a new lane rule and its tests in one experiment branch. Keep it `enabled: false` in committed lane definitions.
3. Use a named forward-paper comparison profile to enable control and experiment **together**. Do not duplicate collectors or create a separate candidate universe.
4. Start a fresh cohort at enablement. Never backfill an experimental lane into older cohorts.
5. Compare only exact shared-candidate / snapshot identities. Missing or mismatched snapshots fail closed; never substitute another cohort.
6. Require independently authoritative resolution rows for PnL. Candidate provenance, `future_pnl_inputs`, labels, and outcome-like fields must never settle a lane.
7. Treat `VOID` as `void_resolution`: retain it for audit, but exclude it from wins, losses, stake, payout, and PnL.
8. Promotion decisions require forward, independently resolved, full-overlap evidence. Historical replay is diagnostic/in-sample evidence, not proof for promotion.

## Data and PnL integrity

- Preserve decision-time inputs only in lane decision provenance. Outcome/settlement metadata belongs in a separate resolution artifact.
- Lane PnL requires valid BUY action/side, decision-time executable fill or entry price, valid hypothetical stake, and an exact independent resolution match.
- Ambiguous market-only resolution matches must fail closed.
- Report integrity blockers and coverage explicitly. Do not silently impute prices, sizes, resolutions, or cohorts.
- Optimize net expected value and payout-aware PnL, not directional win rate alone.

## Testing and review

- Follow test-first development for behavior changes: add a focused failing regression test, then implement the smallest fix.
- Run focused tests for the modified subsystem, then the full test suite before promotion/merge when feasible:

  ```bash
  PYTHONPATH=. .venv/bin/python -m unittest discover -s tests
  git diff --check
  ```

- Treat environment-local ignored configs separately from product failures; tests must not assume an ignored runtime config exists in a clean checkout.
- For non-trivial lane, resolver, replay, or accounting changes, obtain an independent read-only review before merging.

## Reporting expectations

When finishing work, state:

- branch and commit(s) changed;
- whether code merged, was pushed, or remains experimental;
- exact tests run and results;
- runtime/service/config changes (normally: none);
- remaining evidence gaps or blockers.
