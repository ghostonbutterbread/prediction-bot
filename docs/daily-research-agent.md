# Daily research agent runbook

This is a **disabled operating procedure**, not a scheduler. The model may formulate and commit arbitrary version-controlled strategy, lane, composition, test or replay-adapter hypotheses. Existing `auto_research.py` executes only its source-only replay; modifying unrelated code may have no effect. No automatic paper/live promotion, account or service changes. Never run the explicitly prohibited probes or equivalents. Source availability/producer clocks remain unverified; selection and reports cannot establish them. Quotes are not fills.

## Inputs and selection

An operator or separately reviewed adapter supplies a JSON list of absolute stable, immutable, already-projected decision input files and separate outcome files, newest preferred first:

```json
[{"inputs":"/absolute/frozen-inputs.jsonl","outcomes":"/absolute/frozen-outcomes.jsonl"}]
```

No collector discovery or provenance guarantee is provided here. A changed input **or** outcome is a new content pair, not necessarily a new independent opportunity. `select` validates all catalog entries before writing; it bounds individual files, the catalog (default 1 MiB), and aggregate input bytes (default 512 MiB). It retains only one pair in memory at a time; JSON and replay can amplify memory. All paths must be outside the state directory. Limits are operational budgets, not evidence of correctness. Existing selection, frozen hashes and completion artifacts are rechecked; missing/corrupt state fails explicitly and remains for reconciliation. A partially written selection is not automatically resumed. Source files must remain unchanged throughout selection; hashes do not prove producer authority.

```sh
python3 scripts/daily_research.py select --catalog "$CATALOG" --state "$WORK/selections" \
  --max-input-bytes "$INPUT_BYTES" --max-outcome-bytes "$OUTCOME_BYTES" \
  --max-catalog-bytes "$CATALOG_BYTES" --max-total-bytes "$TOTAL_BYTES"
```

For `selected`, use its directory as `D`. For `in_progress`, inspect the prior journal, commits and receipts before resuming; the helper verifies frozen bytes, not the rest of the workflow. For `no_new_data`, the agent may select a previously validated frozen pair deliberately and generate **new hypotheses**; label the journal and all reports `no_new_data`, identify the reused pair, and create a unique new `D` under `$WORK` containing verified copies of its frozen inputs and a fresh runtime. Preserve the original pair's selection and completion and do not call `complete` for this separate hypothesis; retain new CLI receipts and journal there instead. Never overwrite an old pair, receipt, comparison, commit or completion. This is repeated in-sample research, not fresh evidence. Missing or corrupt data is an error, not `no_new_data`.

## Mandatory operating gate before any recurring run

The parent/operator must provision a separate task-owned persistent derived filesystem or project with an **enforced hard aggregate disk quota** covering all `$WORK` files, Git clones, stdout/stderr, container writable bind output, pending artifacts and SQLite files. Confirm quota enforcement and free reserve before every run; stop when exhausted. Monitoring `du`, per-file ulimit, tmpfs size or a planned retention policy is not a hard aggregate bound. No automatic deletion of existing evidence: archive under separately approved policy or stop. Do not mount raw archives, HOME, credentials, Docker socket, accounts, protected roots or other worktrees. The runbook cannot set or verify host project quotas; activation is blocked until the parent verifies this operational prerequisite and the scheduler's output/log budget. Use a fixed local image ID and a clean pinned checkout.

**Exclusive lifetime lock:** `bash scripts/daily_research_guard.sh "$WORK" "$JOB_SECONDS" COMMAND [ARGS...]` holds `flock -n` on `$WORK/.daily.lock` around the entire supervised child, exports the deterministic `CONTAINER` name below, reconciles before and after, and bounds job wall time. `$WORK` must already exist. The child must encompass the **entire** model job and all its subprocesses; wrapping individual short-lived tool calls is insufficient. All jobs sharing this root must cooperate; do not unlink the lock file. If the actual Hermes scheduler cannot run its whole model session as the guarded child, do not activate until an appropriate supervisor implements this contract. The selector itself does not hold a job-wide lock. Quota verification remains an external mandatory gate, not something the guard implements.

**Owned container reconciliation:** the guard hashes the canonical work root to derive a stable name and expects label `org.prediction-bot.daily-owner=$CONTAINER`. Under the lock it checks exactly that name and verifies the label before removal; a mismatch, daemon failure or failed cleanup stops the job. Never remove other containers. SIGKILL or host loss cannot run a trap; the next guard invocation reconciles before doing any work. Record the reconciliation outcome. An unknown concurrent noncooperating job is a blocker, not something to kill. A detached child or noncooperating writer is outside the guard's guarantees.

## Agent workflow under that supervisor

1. Check clean checkout, record checkout path, `HEAD`, tree, image ID, limits, quota verification and reconciliation in a unique bounded journal under `$WORK`. No fetch, reset, push, checkout or config changes in the original checkout.
2. Select as above and journal the exact status. Create two new local task-owned clones under the quota root at the recorded commit (`git clone --no-hardlinks --no-local "$CHECKOUT" ...`; `git checkout --detach "$BASE"`). Verify clean HEAD/tree. Do not edit baseline. Make a named branch in the experiment clone. Scope commit identity **per command** in the isolated environment, without relying on cron HOME or modifying global Git settings:

   `env GIT_AUTHOR_NAME='Daily Research Agent' GIT_AUTHOR_EMAIL='daily-research@localhost.invalid' GIT_COMMITTER_NAME='Daily Research Agent' GIT_COMMITTER_EMAIL='daily-research@localhost.invalid' git commit -m 'Describe hypothesis'`

   Record the commit/tree and tests. Tests first, RED/GREEN; any versioned path may be changed. Do not run arbitrary experiment code on the host.
3. Write a zero-byte file `$D/empty-outcomes.jsonl` with exclusive creation. **Never mount the frozen outcomes, scorer output or other later outcomes into the experiment container.** The current CLI requires an outcome argument, so baseline and candidate both receive this empty file and generate outcome-free decision artifacts. Perform tests in the same isolation. This prevents direct outcome access through these mounts, not indirect leakage already present in source code, training context, logs, or input. Avoid inspecting the separate outcomes before generating/locking decisions. Save each successful run's unmodified stdout as `$D/baseline.json` and `$D/candidate.json`, and stderr separately; require exit status zero and preserve failures.
4. For each run, use a new quota-backed `$D/runtime` containing no outcome or scorer data, the guarded owned container name and the cleanup contract above. Set `CODE` to the relevant clone, `IDEA`, `MAX_ROWS`, `SECONDS_LIMIT`, input/outcome byte limits, and an immutable `IMAGE`. Bound CPU, memory, pids, tmpfs and wall time (including Docker client); no Docker daemon logging growth:

```sh
mkdir -p "$D/runtime/collector/data/derived_reports"
timeout --signal=TERM --kill-after=10s "$SECONDS_LIMIT" docker run --rm --name "$CONTAINER" \
  --label "org.prediction-bot.daily-owner=$CONTAINER" --log-driver none \
  --network none --read-only --tmpfs /tmp:rw,nosuid,nodev,size=256m \
  --cpus 2 --memory 2g --pids-limit 256 --cap-drop ALL --security-opt no-new-privileges \
  --user "$(id -u):$(id -g)" --entrypoint /opt/hermes/.venv/bin/python3 \
  -e HOME=/tmp -e PYTHONDONTWRITEBYTECODE=1 -e PREDICTION_BOT_COLLECTOR_ROOT=/work/runtime/collector \
  --mount "type=bind,src=$CODE,dst=/work/code,readonly" \
  --mount "type=bind,src=$D/inputs.jsonl,dst=/work/inputs.jsonl,readonly" \
  --mount "type=bind,src=$D/empty-outcomes.jsonl,dst=/work/outcomes.jsonl,readonly" \
  --mount "type=bind,src=$D/runtime,dst=/work/runtime" --workdir /work/code "$IMAGE" \
  scripts/auto_research.py run --store /work/runtime/collector/data/derived_reports/research \
  --inputs /work/inputs.jsonl --outcomes /work/outcomes.jsonl --idea "$IDEA" \
  --max-rows "$MAX_ROWS" --max-input-bytes "$INPUT_BYTES" --max-outcome-bytes "$OUTCOME_BYTES"
```

Capture bounded stdout/stderr in quota-backed files, not an unbounded scheduler log. Container writable layers and Docker metadata need a separately verified daemon-level bound or fail the operational gate; the hard quota on `$WORK` does not bound Docker's own storage. Container isolation is not a security proof. On timeout or nonzero exit, preserve partial evidence and stop; cleanup does not mean the experiment completed. The shell supervisor must enforce an outer wall-time bound on the whole model job as well as these command limits.
5. Use the baseline CLI in the same container layout for `compare --store /work/runtime/collector/data/derived_reports/research BASELINE_ID CANDIDATE_ID`, save unmodified stdout as `$D/comparison.json`, check exit status, matched selected rows and all limitations. Comparison verifies registered hashes, but has no independent opportunity roster. Record the hypothesis, code/tests and conclusion in the journal; if failures or no supported change, do not queue. Optional `queue` is manual-review only, never activation.
6. After both runs and comparison have genuinely succeeded, run `python3 scripts/daily_research.py complete --directory "$D" --max-input-bytes "$INPUT_BYTES" --max-outcome-bytes "$OUTCOME_BYTES"`. It checks frozen selection and the three declared receipt JSON files, their identities and comparison's frozen input/empty-outcome hashes, then exclusively publishes hashes in `complete.json`. Future selection rechecks those artifacts and fails on missing/corrupt markers. The completion receipt verifies declared bytes and consistency, **not authenticity**: a user who can forge the store and all receipts can forge these files, and the helper does not execute `compare` or independently validate the SQLite store. Therefore retain the genuine CLI outputs, exit statuses, store, exact code/image and full run artifacts; never hand-create a marker or treat it as independent proof. A pair's completion does not limit further research on that pair.

## Independent late outcome seam

After decisions are sealed, an independently reviewed scorer may read those frozen decision artifacts and separately frozen later outcome evidence in a **different** process and output root. Bind exact decision/snapshot identity and outcome source/availability evidence, report unresolved/conflicting cases and coverage, preserve quote-not-fill and diagnostic-ineligible labels, and never feed scorer outputs back into candidate execution or claim P&L/forward validity without the necessary proof. The scorer exists on a sibling branch and is **not** a dependency or implemented by this runbook. Until integrated and independently verified, write `late_outcome_scoring: unavailable/not_run` in the report; do not fabricate outcome or P&L results. Unchanged history means `no_new_data` even when a new hypothesis is tested.

A separately reviewed version-aware forward adapter and preservation of champion/pending positions are required before any paper handoff. Parent owns integration, independent review, scheduling, quota, cleanup verification and activation.
