#!/usr/bin/env bash

set -euo pipefail

WORKERS="${1:-8}"
USERS="${2:-1200}"
SPAWN_RATE="${3:-240}"
RUN_TIME="${4:-90s}"
shift $(( $# > 4 ? 4 : $# ))

ROOT_DIR="${ROOT_DIR:-$(pwd)}"
# Default to the minimal schedule-driven locustfile.
LOCUSTFILE="${LOCUSTFILE:-$ROOT_DIR/benchmarks/online_boutique/loadgen-locust/locustfile.py}"
HOST="${OB_HOST:-http://127.0.0.1:18081}"
MASTER_HOST="${LOCUST_MASTER_HOST:-127.0.0.1}"
MASTER_PORT="${LOCUST_MASTER_PORT:-5557}"
PROFILE="${OB_PROFILE:-stress_short}"
SCENARIO_ID="${OB_SCENARIO_ID:-ob-stress-distributed-v1}"
LOG_DIR="${LOCUST_LOG_DIR:-$(mktemp -d /tmp/ob-locust-dist.XXXXXX)}"
SHAPE_ENABLED="${OB_ENABLE_DYNAMIC_SHAPE:-0}"
SHAPE_NAME="${OB_DYNAMIC_SHAPE:-curve_6h}"

declare -a WORKER_PIDS=()

cleanup() {
  for pid in "${WORKER_PIDS[@]:-}"; do
    kill "$pid" 2>/dev/null || true
  done
}

trap cleanup EXIT INT TERM

if [[ ! -f "$LOCUSTFILE" ]]; then
  echo "Locustfile not found: $LOCUSTFILE" >&2
  exit 1
fi

export OB_PROFILE="$PROFILE"
export OB_SCENARIO_ID="$SCENARIO_ID"

echo "Starting distributed Locust"
echo "  workers     : $WORKERS"
echo "  users       : $USERS"
echo "  spawn_rate  : $SPAWN_RATE"
echo "  run_time    : $RUN_TIME"
echo "  host        : $HOST"
echo "  profile     : $PROFILE"
echo "  scenario_id : $SCENARIO_ID"
echo "  shape_mode  : $SHAPE_ENABLED"
if [[ "$SHAPE_ENABLED" == "1" ]]; then
  echo "  shape_name  : $SHAPE_NAME"
fi
echo "  logs        : $LOG_DIR"

for i in $(seq 1 "$WORKERS"); do
  locust -f "$LOCUSTFILE" \
    --worker \
    --master-host "$MASTER_HOST" \
    --master-port "$MASTER_PORT" \
    >"$LOG_DIR/worker-$i.log" 2>&1 &
  WORKER_PIDS+=("$!")
done

sleep 2

if [[ "$SHAPE_ENABLED" == "1" ]]; then
  if [[ -z "${OB_SHAPE_DURATION_S:-}" ]]; then
    export OB_SHAPE_DURATION_S="$(
      python - "$RUN_TIME" <<'PY'
import re
import sys
value = sys.argv[1].strip().lower()
match = re.fullmatch(r'(\d+)([smhd])', value)
if not match:
    print(21600)
    raise SystemExit(0)
amount = int(match.group(1))
unit = match.group(2)
scale = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}[unit]
print(amount * scale)
PY
    )"
  fi
  locust -f "$LOCUSTFILE" \
    --master \
    --master-bind-port "$MASTER_PORT" \
    --expect-workers "$WORKERS" \
    --headless \
    --host "$HOST" \
    --run-time "$RUN_TIME" \
    --only-summary \
    "$@"
else
  locust -f "$LOCUSTFILE" \
    --master \
    --master-bind-port "$MASTER_PORT" \
    --expect-workers "$WORKERS" \
    --headless \
    --host "$HOST" \
    -u "$USERS" \
    -r "$SPAWN_RATE" \
    --run-time "$RUN_TIME" \
    --only-summary \
    "$@"
fi
