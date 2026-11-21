#!/usr/bin/env bash
# Hold the task's lock around a complete supervised job, never a single step.
set -u
if (( $# < 3 )); then
  printf 'usage: %s EXISTING_WORK_ROOT WALL_SECONDS COMMAND [ARGS...]\n' "$0" >&2
  exit 2
fi
work=$(realpath -e -- "$1") || exit 2
seconds=$2
shift 2
if [[ ! -d "$work" || ! "$seconds" =~ ^[1-9][0-9]*$ ]]; then
  printf 'existing work directory and positive integer wall seconds required\n' >&2
  exit 2
fi
export CONTAINER="daily-research-$(printf %s "$work" | sha256sum | cut -c1-20)"
exec 9>"$work/.daily.lock" || exit 1
if ! flock -n 9; then
  printf 'daily research lock is held: %s\n' "$work" >&2
  exit 1
fi
reconcile() {
  local names label
  names=$(docker container ls -a --filter "name=^/${CONTAINER}$" --format '{{.Names}}') || {
    printf 'container enumeration failed; reconciliation required\n' >&2; return 1;
  }
  [[ -z "$names" ]] && return 0
  if [[ "$names" != "$CONTAINER" ]]; then
    printf 'unexpected container names; reconciliation required\n' >&2; return 1
  fi
  label=$(docker inspect --format '{{ index .Config.Labels "org.prediction-bot.daily-owner" }}' "$CONTAINER") || return 1
  if [[ "$label" != "$CONTAINER" ]]; then
    printf 'container owner label mismatch; refusing cleanup\n' >&2; return 1
  fi
  docker rm -f "$CONTAINER" >/dev/null || {
    printf 'owned container cleanup failed; reconciliation required\n' >&2; return 1;
  }
}
reconcile || exit 1
child=''
interrupted=0
interrupt() {
  interrupted=1
  [[ -z "$child" ]] || kill -TERM "$child" 2>/dev/null || true
}
trap interrupt INT TERM HUP
timeout --signal=TERM --kill-after=10s "$seconds" "$@" &
child=$!
wait "$child"
status=$?
if (( interrupted )); then
  wait "$child" 2>/dev/null || true
  status=130
fi
trap - INT TERM HUP
if ! reconcile; then
  printf 'daily research failed cleanup; preserve evidence and reconcile\n' >&2
  exit 1
fi
exit "$status"
