# Daily research agent runbook

**Disabled procedure; no scheduler or activation is installed here.** This is cooperative ownership, not a sandbox or a whole-model wall-time supervisor. Native Hermes cron runs the model itself, not beneath `daily_research_guard.sh`; do not claim otherwise. The agent may change any versioned path in its candidate branch. Never place live orders, touch accounts, services or raw collector artifacts, run prohibited probes, or automatically promote. Source-time and independent outcomes are diagnostic-only; quotes are not fills.

## Claim, budget and inputs

The operator supplies an existing exclusive derived work root `$WORK`, clean trusted checkout `$TRUSTED` at a reviewed commit containing this runbook and CLI, a fixed local image ID `$IMAGE`, and a JSON catalog `$CATALOG` of absolute, immutable, already-projected input/outcome file pairs, newest first. No new collector or SourceWriter is required. Set explicit positive `INPUT_BYTES`, `OUTCOME_BYTES`, `CATALOG_BYTES`, `TOTAL_BYTES`, `MAX_ROWS`, `SECONDS_LIMIT`, `RUN_GROWTH_BYTES`, `MIN_FREE_BYTES`, and `LOG_BLOCKS` (shell `ulimit -f` units) before each run. Record these, image ID, trusted HEAD/tree, journal path and input sources. These are declared per-run budgets, not a hard aggregate quota or storage-policy change. No automatic deletion; stop and seek an authorized archival/retention decision when necessary. Bound CPU, memory, PIDs, per-container wall time, output files, and daemon logs; monitor work-root growth and filesystem free space before and after every expensive operation and stop on exhaustion. Sampling can miss growth between checks, daemon metadata, and other writers. `ulimit -f` below is a per-file limit, not aggregate enforcement. Size/retention of scheduler logs must also be considered by the operator.

At the very beginning of **each native model session**, before selection or other writes, generate a unique operator-visible owner ID, then run:

```sh
OWNER="$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"
python3 "$TRUSTED/scripts/daily_research.py" claim --work "$WORK" --owner "$OWNER"
```

Journal the claim receipt and owner ID. A pre-existing claim, including a partial, stale or unknown one, is a failure: report `[CRON_FAILURE]`, preserve it and stop. Do not infer safety from the scheduler's status or automatically steal or clear it. An operator must establish all model/tool/container processes are stopped, reconcile partial evidence and exact owned containers, preserve the claim and record the decision before manually moving it aside. The short `flock` serializes claim/finish operations; the durable claim spans independent tool calls. It only coordinates cooperating jobs with the same stable trusted work root and filesystem; it does not prevent arbitrary agent code bypassing the protocol. Never unlink its lock. On a clean terminal result, after all containers are stopped and artifacts journaled, call `finish --work "$WORK" --owner "$OWNER"`; it atomically moves the claim to `claim-history`. On crash, uncertainty or incomplete cleanup leave it in place. Any failed command or budget check must end the model response with `[CRON_FAILURE]` and a concise reconciliation status; never silently report success. `finish` is not proof of scheduler termination.

Example sampled budget check, with `START_BYTES` recorded from `du -sb "$WORK"` at run start (all paths are operator-owned):

```sh
USED=$(du -sb "$WORK" | cut -f1)
FREE=$(df -PB1 "$WORK" | awk 'NR==2 {print $4}')
test "$((USED - START_BYTES))" -lt "$RUN_GROWTH_BYTES" && test "$FREE" -ge "$MIN_FREE_BYTES" || { printf '%s\n' '[CRON_FAILURE] budget exhausted'; exit 1; }
```

Check before and after each clone, test, replay, compare and publication, and periodically during longer work; terminate the owned workload when a limit is reached. No check proves a hard limit between samples. Capture bounded logs in work storage instead of unbounded scheduler output.

Catalog format: `[{"inputs":"/absolute/frozen-inputs.jsonl","outcomes":"/absolute/frozen-outcomes.jsonl"}]`. Catalog sources must not be under the selection state. Selection checks the entire bounded catalog before writes and hashes frozen inputs and outcomes; hashes do not authenticate origin or timestamps.

```sh
python3 "$TRUSTED/scripts/daily_research.py" select --catalog "$CATALOG" --state "$WORK/selections" \
  --max-input-bytes "$INPUT_BYTES" --max-outcome-bytes "$OUTCOME_BYTES" \
  --max-catalog-bytes "$CATALOG_BYTES" --max-total-bytes "$TOTAL_BYTES"
```

Save stdout and take the returned directory as `D`. For `in_progress`, reconcile the journal, commits and artifacts before resuming; partial/corrupt state is never automatically repaired. For `no_new_data`, deliberately choose a previously verified frozen pair, make a separate unique research directory with verified copies and label all results `no_new_data`; do not overwrite or complete the original pair. New hypotheses remain allowed but are in-sample. Never inspect or mount the frozen outcomes during candidate execution.

## Concrete isolated execution and trusted comparison

The following is a sequence for a *selected* pair, not an unattended shell supervisor. Perform budget checks around each step. Use a fresh `$RUN` beneath `$WORK` for this claim, never an existing directory. It contains baseline and candidate stores which must be distinct and non-overlapping. `$TRUSTED` must remain clean and immutable throughout and its code, not candidate code, executes comparison. No source, HOME, accounts, credentials, Docker socket or other stores are mounted into candidate execution. Docker isolation is not a guarantee against malicious code, indirect outcome leakage in input, image or model context, or a noncooperating host agent.

```sh
BASE=$(git -C "$TRUSTED" rev-parse HEAD)
mkdir "$RUN"
git clone --no-hardlinks --no-local "$TRUSTED" "$RUN/baseline-code"
git -C "$RUN/baseline-code" checkout --detach "$BASE"
git clone --no-hardlinks --no-local "$TRUSTED" "$RUN/candidate-code"
git -C "$RUN/candidate-code" checkout -b "daily-$OWNER" "$BASE"
mkdir -p "$RUN/baseline/collector/data/derived_reports/research" "$RUN/candidate/collector/data/derived_reports/research"
: > "$RUN/empty-outcomes.jsonl"
```

Generate a hypothesis and modify/test/commit candidate code only in its isolated clone; record RED/GREEN evidence, changed paths, commit and tree. Scope Git identity to the commit command, e.g. `env GIT_AUTHOR_NAME='Daily Research Agent' GIT_AUTHOR_EMAIL='daily-research@localhost.invalid' GIT_COMMITTER_NAME='Daily Research Agent' GIT_COMMITTER_EMAIL='daily-research@localhost.invalid' git -C "$RUN/candidate-code" commit -m 'Describe hypothesis'`. Do not run arbitrary candidate code on the host. If the test harness cannot be safely isolated, stop.

For each `SIDE` in `baseline candidate`, set `CODE="$RUN/$SIDE-code"` and `RUNTIME="$RUN/$SIDE"`; use the same replay settings for both. Run a uniquely named container, capture successful JSON stdout as `$D/$SIDE.json` and bounded stderr separately. The candidate receives only its own writable runtime; baseline is not mounted at all. A zero-byte outcomes file is the only outcome mount. The outer native agent is not wrapped by the timeout below; this bounds only the Docker command. Reconcile the exact named container and ownership label before and after each run, and after interruptions; do not remove unrelated containers or proceed on unknown ownership. Use the existing `daily_research_guard.sh` only for an individual command if its label reconciliation is desired, never as proof that it owns the entire model session.

```sh
SIDE=baseline # repeat with candidate after the candidate commit
CODE="$RUN/$SIDE-code"; RUNTIME="$RUN/$SIDE"; CONTAINER="daily-$OWNER-$SIDE"
(ulimit -f "$LOG_BLOCKS"; timeout --signal=TERM --kill-after=10s "$SECONDS_LIMIT" docker run --name "$CONTAINER" \
  --label "org.prediction-bot.daily-owner=$CONTAINER" --log-driver none --network none --read-only \
  --tmpfs /tmp:rw,nosuid,nodev,size=256m --cpus 2 --memory 2g --pids-limit 256 \
  --cap-drop ALL --security-opt no-new-privileges --user "$(id -u):$(id -g)" \
  --entrypoint /opt/hermes/.venv/bin/python3 -e HOME=/tmp -e PYTHONDONTWRITEBYTECODE=1 \
  -e PREDICTION_BOT_COLLECTOR_ROOT=/work/runtime/collector \
  --mount "type=bind,src=$CODE,dst=/work/code,readonly" \
  --mount "type=bind,src=$D/inputs.jsonl,dst=/work/inputs.jsonl,readonly" \
  --mount "type=bind,src=$RUN/empty-outcomes.jsonl,dst=/work/outcomes.jsonl,readonly" \
  --mount "type=bind,src=$RUNTIME,dst=/work/runtime" --workdir /work/code "$IMAGE" \
  scripts/auto_research.py run --store /work/runtime/collector/data/derived_reports/research \
  --inputs /work/inputs.jsonl --outcomes /work/outcomes.jsonl --idea "$SIDE hypothesis" \
  --max-rows "$MAX_ROWS" --max-input-bytes "$INPUT_BYTES" --max-outcome-bytes "$OUTCOME_BYTES") \
  > "$D/$SIDE.json" 2> "$RUN/$SIDE.stderr"
```

Inspect exit status and exact container state before removing only the matching named/labelled stopped container. On timeout or nonzero exit, retain evidence and claim, report `[CRON_FAILURE]` and stop. Check clean clone HEAD/tree and validate returned commit equals its expected HEAD. After both have stopped, record and verify the baseline store hash manifest before and after candidate execution; a changed baseline is a failure. The mounts—not hashes—prevent the candidate container from writing the baseline; hashes detect later alteration under the cooperating host assumption.

Immediately after the baseline and before starting the candidate, save a sorted manifest outside either mounted store, then check it after the candidate:

```sh
(cd "$RUN/baseline/collector/data/derived_reports/research" && find . -type f -print0 | sort -z | xargs -0 sha256sum) > "$RUN/baseline.sha256"
# Run candidate and reconcile its container here.
(cd "$RUN/baseline/collector/data/derived_reports/research" && sha256sum -c "$RUN/baseline.sha256")
```

Also compare a freshly generated manifest with the original to detect added files; stop on any difference. These are snapshots under the stable-root/cooperating-writer assumption, not an audit against malicious host writers.

Run comparison in a new isolated process with **trusted clean code** and both stores mounted **read-only**, never with candidate code and never with either store writable. The roots are mapped beneath the same collector derived root; `--candidate-store` remaps registered run paths to their respective mounted stores. Capture stdout as `$D/comparison.json`; inspect status and `selected_rows_matched`, identity/coverage and diagnostic warnings.

```sh
BID=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["experiment_id"])' "$D/baseline.json")
CID=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["experiment_id"])' "$D/candidate.json")
CONTAINER="daily-$OWNER-compare"
(ulimit -f "$LOG_BLOCKS"; timeout --signal=TERM --kill-after=10s "$SECONDS_LIMIT" docker run --name "$CONTAINER" \
  --label "org.prediction-bot.daily-owner=$CONTAINER" --log-driver none --network none --read-only \
  --tmpfs /tmp:rw,nosuid,nodev,size=256m --cpus 2 --memory 2g --pids-limit 256 \
  --cap-drop ALL --security-opt no-new-privileges --user "$(id -u):$(id -g)" \
  --entrypoint /opt/hermes/.venv/bin/python3 -e HOME=/tmp -e PYTHONDONTWRITEBYTECODE=1 \
  -e PREDICTION_BOT_COLLECTOR_ROOT=/compare/collector \
  --mount "type=bind,src=$TRUSTED,dst=/work/code,readonly" \
  --mount "type=bind,src=$RUN/baseline/collector/data/derived_reports/research,dst=/compare/collector/data/derived_reports/baseline,readonly" \
  --mount "type=bind,src=$RUN/candidate/collector/data/derived_reports/research,dst=/compare/collector/data/derived_reports/candidate,readonly" \
  --workdir /work/code "$IMAGE" scripts/auto_research.py compare \
  --store /compare/collector/data/derived_reports/baseline \
  --candidate-store /compare/collector/data/derived_reports/candidate "$BID" "$CID") \
  > "$D/comparison.json" 2> "$RUN/compare.stderr"
```

The existing comparator verifies registered artifact hashes, identical input/outcome hashes and replay settings, and exact decision identities. Candidate-generated database entries and hashes are consistency assertions, not authentication of candidate behavior or producer authority. The result is selected-row comparison, not an independent opportunity roster or promotion evidence. Do not let either process change stores during comparison. Retain both stores, logs, commits and journal. A successful actual compare and two genuine receipts are required before:

```sh
python3 "$TRUSTED/scripts/daily_research.py" complete --directory "$D" \
  --max-input-bytes "$INPUT_BYTES" --max-outcome-bytes "$OUTCOME_BYTES"
python3 "$TRUSTED/scripts/daily_research.py" finish --work "$WORK" --owner "$OWNER"
```

Completion rechecks selected bytes and receipt hashes/identity; it does not authenticate the comparison or execute it. Dangling or tampered markers fail closed. Never hand-create a receipt. Follow container cleanup and budget checks before finish. Optional queue is manual-review only; there is no automatic paper/live promotion.

## Independent late outcome seam

Only after decisions are sealed may a separately reviewed scorer consume frozen decisions and separately frozen outcomes in a different process and root. Bind exact decision/snapshot identity and outcome availability evidence; retain unresolved/conflicts and quote-not-fill labels. Until integrated, report `late_outcome_scoring: unavailable/not_run`. The sibling scorer is not implemented here. Parent owns integration, independent review, any scheduler configuration and activation.
