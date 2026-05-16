#!/usr/bin/env bash
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_REPO="$(cd "$SCRIPT_DIR/.." && pwd)"

REPO_CANDIDATES=(
  "$SCRIPT_REPO"
  "/home/ryushe/projects/prediction-bot"
  "/home/ryushe/active-projects/prediction-bot"
)
REPO=""
for candidate in "${REPO_CANDIDATES[@]}"; do
  if [[ -d "$candidate" ]]; then
    REPO="$candidate"
    break
  fi
done

if [[ -z "$REPO" ]]; then
  echo "prediction_lab_monitor_cron: no prediction-bot repo found" >&2
  exit 1
fi

CONFIG_PATH="config.prediction_lab_weather_overnight.yaml"
STATE_FILE="data/paper/prediction_lab/monitor_state.json"
LOCK="data/paper/prediction_lab/monitor.lock"
LOG="data/paper/prediction_lab/logs/monitor_cron.log"

# If the beta-shadow collector is intentionally active or has an existing
# shadow state/pid file, monitor that isolated project. This keeps an exited
# beta-shadow collector from falling back to stale normal-runtime alerts.
if [[ -f "$REPO/config.prediction_lab_beta_shadow_weather.yaml" ]] \
  && { pgrep -af '^python3 scripts/prediction_lab_collect.py --config config.prediction_lab_beta_shadow_weather.yaml --observer' >/dev/null 2>&1 \
    || [[ -f "$REPO/data/beta_shadow/paper/prediction_lab/state.json" ]] \
    || [[ -f "$REPO/data/beta_shadow/paper/prediction_lab/collector.pid" ]]; }; then
  CONFIG_PATH="config.prediction_lab_beta_shadow_weather.yaml"
  STATE_FILE="data/beta_shadow/paper/prediction_lab/monitor_state.json"
  LOCK="data/beta_shadow/paper/prediction_lab/monitor.lock"
  LOG="data/beta_shadow/paper/prediction_lab/logs/monitor_cron.log"
elif [[ ! -f "$REPO/$CONFIG_PATH" && -f "$REPO/config.yaml" ]]; then
  CONFIG_PATH="config.yaml"
fi

export PATH="/home/linuxbrew/.linuxbrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
export OPENCLAW_BIN="${OPENCLAW_BIN:-/home/linuxbrew/.linuxbrew/bin/openclaw}"

mkdir -p "$(dirname "$REPO/$LOCK")" "$(dirname "$REPO/$LOG")"
cd "$REPO" || exit 1

# Avoid overlapping checks if the host is slow.
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "$(date -Is) monitor already running; skipping" >> "$LOG"
  exit 0
fi

{
  echo "--- $(date -Is) prediction_lab_monitor start config=$CONFIG_PATH ---"
  monitor_args=(
    --config "$CONFIG_PATH"
    --state-file "$STATE_FILE"
    --notify
    --target -1003763915138
    --thread-id 8
  )
  if [[ -n "${PREDICTION_LAB_REPAIR_CRON_JOB_ID:-}" ]]; then
    monitor_args+=(--repair-cron-job-id "$PREDICTION_LAB_REPAIR_CRON_JOB_ID")
  fi

  PYTHONPATH=. python3 scripts/prediction_lab_monitor.py "${monitor_args[@]}"
  status=$?
  echo "--- $(date -Is) prediction_lab_monitor exit=$status ---"
  exit "$status"
} >> "$LOG" 2>&1
