# SourceWriter configurable output root

- **Feature branch:** `fix/sourcewriter-configurable-output-root`
- **Base/target:** `4ca50c3` on `beta` → `beta` (not `main`)
- **Contract:** `/mnt/data-collection/prediction-bot` remains the default through `PREDICTION_BOT_COLLECTOR_ROOT`; `--output-root` may name any new or empty filesystem location, including a checkout/root-disk location.
- **Scope:** remove the derived-root allowlist from SourceWriter’s internal export/binding/replay helpers while retaining their non-empty-directory overwrite protection. No scheduler, activation, retention, or artifact cleanup change.
- **Evidence:** focused 57-test SourceWriter/helper suite, `py_compile`, and `git diff --check` pass.
- **Next:** review finding resolved; commit, merge to `beta` only, then remove this branch-local dossier from the target.