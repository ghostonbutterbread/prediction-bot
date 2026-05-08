#!/usr/bin/env bash
set -uo pipefail

REPO_CANDIDATES=(
  "/home/ryushe/active-projects/prediction-bot"
  "/home/ryushe/projects/prediction-bot"
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
if [[ "$REPO" == "/home/ryushe/active-projects/prediction-bot" && -f "$REPO/config.yaml" ]]; then
  CONFIG_PATH="config.yaml"
elif [[ ! -f "$REPO/$CONFIG_PATH" && -f "$REPO/config.yaml" ]]; then
  CONFIG_PATH="config.yaml"
fi

LOCK="$REPO/data/paper/prediction_lab/monitor.lock"
LOG="$REPO/data/paper/prediction_lab/logs/monitor_cron.log"

export PATH="/home/linuxbrew/.linuxbrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
export OPENCLAW_BIN="${OPENCLAW_BIN:-/home/linuxbrew/.linuxbrew/bin/openclaw}"

mkdir -p "$(dirname "$LOCK")" "$(dirname "$LOG")"
cd "$REPO" || exit 1

# Avoid overlapping checks if the host is slow.
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "$(date -Is) monitor already running; skipping" >> "$LOG"
  exit 0
fi

{
  echo "--- $(date -Is) prediction_lab_monitor start ---"
  PYTHONPATH=. python3 scripts/prediction_lab_monitor.py \
    --config "$CONFIG_PATH" \
    --state-file data/paper/prediction_lab/monitor_state.json \
    --notify \
    --target -1003763915138 \
    --thread-id 8 \
    --repair-cron-job-id c4dc2e07-df12-4cc2-8150-1b5221d9e383
  status=$?
  echo "--- $(date -Is) prediction_lab_monitor exit=$status ---"
  exit "$status"
} >> "$LOG" 2>&1
