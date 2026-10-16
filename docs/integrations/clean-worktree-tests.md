# Clean-worktree test prerequisite integration dossier

- **Status:** independent re-review approved scoped local beta integration; publication pending separate lane decision
- **Owner:** Hermes delegated repair
- **Branch / owning feature ref:** `fix/clean-worktree-tests`
- **Base commit:** `3911f1808828261d8cfe712144e881c5b0882351` (selected local `beta`, fetched `origin/beta` was ancestor)
- **Intended integration target:** `beta` (no merge or push authorized for this task)
- **Last updated:** 2026-10-15
- **Latest immutable recovery checkpoint:** none before this commit; inspect branch tip
- **Feature implementation commit(s):** this dossier and README are committed together at branch tip
- **Inspiration / canonical references:** PC-20260819-021915-0436b459, PC-20260823-025851-95199b0b, PC-20260823-032419-f5051a56, PC-20260823-215810-b0111f0d, PC-20260824-051244-3c50c4af, PC-20260826-213812-06da49c0, PC-20260828-041207-fc74b9c7; `README.md`, `AGENTS.md`, `setup.sh`, `requirements.txt`.

## Intent and implemented contract

Document a repeatable test bootstrap in each clean worktree without copying another checkout's ignored `.venv`. `setup.sh` uses the `PYTHON` override; `kalshi_python_sync==3.9.0` requires Python 3.13. README Quick Start and test section now supply the prerequisite, interpreter check, and focused import-safe unittest command. The single root requirements manifest declares `requests` (used by test patch targets) and `rich` (imported by the dashboard), absent from the first clean-worktree full suite. No runtime config, ledger, service, or lane changes. This does not repair every historical replay launcher that hard-codes another interpreter.

## Evidence and review

- Fresh isolated worktree had no `.venv/bin/python`; existing `AGENTS.md` command failed with exit 127.
- `./setup.sh` with default Python 3.11 failed resolving `kalshi_python_sync==3.9.0` (available matching release requires 3.13); Python 3.13 is installed locally under uv's managed interpreters.
- A fresh `python3.13 -m venv` scratch probe included pip. In the feature worktree, `uv venv --python 3.13 --seed --clear .venv` then `PYTHON=/home/ryushe/.local/share/uv/python/cpython-3.13-linux-x86_64-gnu/bin/python3.13 ./setup.sh` installed the complete root requirements successfully. Worktree-local Python reports 3.13.12; `bot.__file__` resolved within this worktree.
- `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -p 'test_runtime_paths.py' -v`: 4 passed.
- `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -p 'test_collector_replay_inputs.py' -q`: 9 passed. The exact documented command with `PYTHONPATH=.` and `-v` also passed 9 tests.
- Initial `git diff --check` passed. Initial review scope was README and this temporary dossier; follow-up also changes root `requirements.txt`. No generated/ignored runtime files staged.
- Independent first review found Quick Start still used default Python 3.11 and the checkout-local full suite had 18 missing-dependency errors across 1,423 tests (15 `rich`, 3 `requests`; 7 skipped). It found no contradiction in `AGENTS.md` after README's prerequisite, but withheld integration approval.
- Follow-up Quick Start uses `PYTHON=python3.13 ./setup.sh`. Re-ran the repo installer with the existing Python 3.13 checkout-local `.venv` after adding `requests>=2.32` and `rich>=13` to `requirements.txt`; installed versions resolved to requests 2.34.2 and rich 15.0.0. `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. .venv/bin/python -m unittest discover -s tests -q` then passed **1,423 tests (7 skipped)** in 148.892s, exit 0. No real trading or service action was requested; suite uses synthetic/mocked fixtures. `git diff --check` passed.
- Independent follow-up reviewer approved the final README/requirements change for **scoped local beta integration**. The checkout-local Python 3.13 `.venv` independently passed 1,423 tests (7 skipped), `pip check`, the nine-test documented focused command, and diff checks. This does not approve publishing 22 unrelated local beta commits.
- An edit to protected `AGENTS.md` was explicitly denied by the approval gate; do not retry it without fresh user authorization. Its test command assumes an already-provisioned `.venv` but is valid after the README bootstrap. README is the editable canonical setup guidance for this scoped repair.
- Merge/ancestry: local `origin/beta` was an ancestor of selected `beta` at branch creation. No merge, push, or papercut closure attempted.

## Blockers and deferred work

- **Missing evidence:** local beta integration and its merged-tree checks; remote publication requires a separate decision about unrelated local beta commits. `AGENTS.md` still shows the test command without setup prerequisite; after README Quick Start that command is valid, but agents reading only AGENTS may still miss provisioning.
- **Command / environment:** `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. .venv/bin/python -m unittest discover -s tests -q`; an authorized owner must request/approve any future `AGENTS.md` edit. Do not retry the denied edit in this task.
- **Trigger:** parent-controlled scoped local `beta` integration, then explicit remote lane disposition; separate owner authorization for protected-file reconciliation if desired.
- **Why:** selected local `beta` is 22 commits ahead of fetched `origin/beta`; publishing that unrelated history requires an explicit lane decision, not a default push.
- **Next completion step:** parent reviews final diff and repository lane/approval boundary. Papercuts remain open until integrated verification.

## Interruption / resume handoff

- **Owning feature ref:** `fix/clean-worktree-tests`
- **Latest immutable recovery checkpoint:** branch tip commit containing README and dossier (record SHA in handoff after commit).
- **Exact resume point:** parent integrates approved scoped change into clean local beta and reruns checks; do not push unrelated history or retry protected edit without authorization.
- **Working-tree state at handoff:** expected clean aside from ignored checkout-local `.venv`.

## Decision gates

- **Integration:** independent review approved local beta integration; merged-tree checks remain.
- **Activation / cohort:** none; docs-only test setup.
- **Promotion:** not requested.

## Decision record

- 2026-10-15 — documented clean-worktree Python 3.13 setup and focused local test command; protected `AGENTS.md` edit refused.
- 2026-10-15 — first review found missing Quick Start override and undeclared test/runtime imports; added two manifest dependencies, resynced through setup.sh, full checkout-local suite green; no beta integration.
- 2026-10-15 — independent re-review approved scoped local beta integration after 1,423 tests (7 skipped), `pip check`, focused command and diff checks; remote publication of unrelated beta commits was not approved.
