#!/usr/bin/env bash
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_REPO="$(cd "$SCRIPT_DIR/.." && pwd)"

REPO_CANDIDATES=(
  "$SCRIPT_REPO"
  "/mnt/data-collection/prediction-bot"
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

MONITOR_PROFILE="${PREDICTION_LAB_MONITOR_PROFILE:-stable}"
case "$MONITOR_PROFILE" in
  stable|normal|paper)
    ;;
  beta_shadow)
    # Beta-shadow monitoring is opt-in. Prefer the currently active limited
    # runtime config when present so process matching follows the launched
    # collector command exactly instead of the base template config.
    if [[ -f "$REPO/data/runtime_configs/prediction_lab_limited_shadow_20260516.yaml" ]]; then
      CONFIG_PATH="data/runtime_configs/prediction_lab_limited_shadow_20260516.yaml"
      STATE_FILE="data/beta_shadow/paper/prediction_lab/monitor_state.json"
      LOCK="data/beta_shadow/paper/prediction_lab/monitor.lock"
      LOG="data/beta_shadow/paper/prediction_lab/logs/monitor_cron.log"
    elif [[ -f "$REPO/config.prediction_lab_beta_shadow_weather.yaml" ]]; then
      CONFIG_PATH="config.prediction_lab_beta_shadow_weather.yaml"
      STATE_FILE="data/beta_shadow/paper/prediction_lab/monitor_state.json"
      LOCK="data/beta_shadow/paper/prediction_lab/monitor.lock"
      LOG="data/beta_shadow/paper/prediction_lab/logs/monitor_cron.log"
    else
      echo "prediction_lab_monitor_cron: beta_shadow profile requested but beta config missing" >&2
      exit 1
    fi
    ;;
  *)
    echo "prediction_lab_monitor_cron: unsupported PREDICTION_LAB_MONITOR_PROFILE=$MONITOR_PROFILE" >&2
    exit 1
    ;;
esac

if [[ ! -f "$REPO/$CONFIG_PATH" && -f "$REPO/config.yaml" ]]; then
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
