#!/usr/bin/env bash
set -uo pipefail

REPO="/home/ryushe/projects/prediction-bot"
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
    --config config.prediction_lab_weather_overnight.yaml \
    --state-file data/paper/prediction_lab/monitor_state.json \
    --notify \
    --target -1003763915138 \
    --thread-id 8 \
    --repair-cron-job-id c4dc2e07-df12-4cc2-8150-1b5221d9e383
  status=$?
  echo "--- $(date -Is) prediction_lab_monitor exit=$status ---"
  exit "$status"
} >> "$LOG" 2>&1
