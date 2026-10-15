# Clean-worktree test prerequisite integration dossier

- **Status:** feature (local handoff; not integrated)
- **Owner:** Hermes delegated repair
- **Branch / owning feature ref:** `fix/clean-worktree-tests`
- **Base commit:** `3911f1808828261d8cfe712144e881c5b0882351` (selected local `beta`, fetched `origin/beta` was ancestor)
- **Intended integration target:** `beta` (no merge or push authorized for this task)
- **Last updated:** 2026-10-15
- **Latest immutable recovery checkpoint:** none before this commit; inspect branch tip
- **Feature implementation commit(s):** this dossier and README are committed together at branch tip
- **Inspiration / canonical references:** PC-20260819-021915-0436b459, PC-20260823-025851-95199b0b, PC-20260823-032419-f5051a56, PC-20260823-215810-b0111f0d, PC-20260824-051244-3c50c4af, PC-20260826-213812-06da49c0, PC-20260828-041207-fc74b9c7; `README.md`, `AGENTS.md`, `setup.sh`, `requirements.txt`.

## Intent and implemented contract

Document a repeatable test bootstrap in each clean worktree without copying another checkout's ignored `.venv`. `setup.sh` uses the `PYTHON` override; `kalshi_python_sync==3.9.0` requires Python 3.13. README now supplies the bootstrap, local interpreter check, and focused import-safe unittest command. No dependency, runtime config, ledger, service, or lane changes. This does not repair every historical replay launcher that hard-codes another interpreter.

## Evidence and review

- Fresh isolated worktree had no `.venv/bin/python`; existing `AGENTS.md` command failed with exit 127.
- `./setup.sh` with default Python 3.11 failed resolving `kalshi_python_sync==3.9.0` (available matching release requires 3.13); Python 3.13 is installed locally under uv's managed interpreters.
- A fresh `python3.13 -m venv` scratch probe included pip. In the feature worktree, `uv venv --python 3.13 --seed --clear .venv` then `PYTHON=/home/ryushe/.local/share/uv/python/cpython-3.13-linux-x86_64-gnu/bin/python3.13 ./setup.sh` installed the complete root requirements successfully. Worktree-local Python reports 3.13.12; `bot.__file__` resolved within this worktree.
- `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -p 'test_runtime_paths.py' -v`: 4 passed.
- `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -p 'test_collector_replay_inputs.py' -q`: 9 passed. The exact documented command with `PYTHONPATH=.` and `-v` also passed 9 tests.
- `git diff --check` passed. Reviewed scope: README documentation and this temporary dossier only; no generated/ignored runtime files staged.
- An edit to protected `AGENTS.md` was explicitly denied by the approval gate; do not retry it without fresh user authorization. Its stale `.venv` assumption remains. README is the editable canonical setup guidance for this scoped repair.
- Merge/ancestry: local `origin/beta` was an ancestor of selected `beta` at branch creation. No merge, push, or papercut closure attempted.

## Blockers and deferred work

- **Missing evidence:** full test suite, independent integration review, and update to protected `AGENTS.md` test guidance.
- **Command / environment:** `PYTHONPATH=. .venv/bin/python -m unittest discover -s tests`; an authorized owner must request/approve any future `AGENTS.md` edit.
- **Trigger:** before a proposed `beta` integration, or upon explicit owner authorization for the protected file.
- **Why:** this task stops at a reviewed-scope feature commit; a stale instruction remains visible and must be reconciled by an authorized actor before claiming comprehensive guidance repair.
- **Next completion step:** parent agent reviews branch diff and approval boundary, reruns tests if proceeding, resolves `AGENTS.md` separately, and decides integration. Papercuts remain open until actual integrated verification.

## Interruption / resume handoff

- **Owning feature ref:** `fix/clean-worktree-tests`
- **Latest immutable recovery checkpoint:** branch tip commit containing README and dossier (record SHA in handoff after commit).
- **Exact resume point:** inspect branch tip and scoped diff; decide whether protected guidance can be edited and whether to integrate into selected `beta`.
- **Working-tree state at handoff:** expected clean aside from ignored checkout-local `.venv`.

## Decision gates

- **Integration:** not performed; requires independent review, latest upstream comparison, and appropriate tests.
- **Activation / cohort:** none; docs-only test setup.
- **Promotion:** not requested.

## Decision record

- 2026-10-15 — documented clean-worktree Python 3.13 setup and focused local test command; protected `AGENTS.md` edit refused; keep feature branch for review without integration.
