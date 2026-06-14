#!/usr/bin/env bash

set -euo pipefail

SCHEDULE_FILE="${1:-}"
WORKERS="${2:-12}"
RUN_TIME="${3:-14d}"
shift $(( $# > 3 ? 3 : $# ))

if [[ -z "$SCHEDULE_FILE" ]]; then
  echo "Usage: $0 <schedule_csv> [workers] [run_time] [locust args...]" >&2
  exit 1
fi

ROOT_DIR="${ROOT_DIR:-$(pwd)}"
RUN_ID="${OB_RUN_ID:-unknown}"
PROFILE="${OB_PROFILE:-day_normal}"
SCENARIO_ID="${OB_SCENARIO_ID:-ob-traffic-schedule-10s-v1}"
RUN_ARTIFACT_DIR="${OB_RUN_ARTIFACT_DIR:-}"

timestamp_utc() {
  date -u +"%Y-%m-%dT%H:%M:%SZ"
}

append_injection_event() {
  local event_name="$1"
  local event_time="$2"
  shift 2
  [[ -n "${OB_INJECTION_EVENTS_FILE:-}" ]] || return 0
  python - "$OB_INJECTION_EVENTS_FILE" "$event_name" "$event_time" "$RUN_ID" "$SCENARIO_ID" "$@" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
event_name = sys.argv[2]
event_time = sys.argv[3]
run_id = sys.argv[4]
scenario_id = sys.argv[5]
extra = {}
items = sys.argv[6:]
if len(items) % 2 != 0:
    raise SystemExit("append_injection_event expects key/value pairs")
for i in range(0, len(items), 2):
    key = items[i]
    raw_value = items[i + 1]
    try:
        extra[key] = json.loads(raw_value)
    except json.JSONDecodeError:
        extra[key] = raw_value

path.parent.mkdir(parents=True, exist_ok=True)
payload = {
    "event": event_name,
    "utc": event_time,
    "run_id": run_id,
    "scenario_id": scenario_id,
}
payload.update(extra)
with path.open("a", encoding="utf-8") as f:
    f.write(json.dumps(payload, ensure_ascii=True) + "\n")
PY
}

update_run_manifest() {
  [[ -n "${OB_RUN_MANIFEST_PATH:-}" ]] || return 0
  python - "$OB_RUN_MANIFEST_PATH" "$1" "$RUN_ID" "$SCENARIO_ID" "$OB_SHAPE_FILE" "$RUN_TIME" "$WORKERS" "$PROFILE" "$2" <<'PY'
import csv
import json
import sys
from pathlib import Path

manifest_path = Path(sys.argv[1])
status = sys.argv[2]
run_id = sys.argv[3]
scenario_id = sys.argv[4]
schedule_path = Path(sys.argv[5])
run_time = sys.argv[6]
workers = int(sys.argv[7])
profile = sys.argv[8]
updates = json.loads(sys.argv[9])

step_s = None
first_t_s = None
last_t_s = None
points = 0
if schedule_path.exists():
    with schedule_path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = []
        for row in reader:
            t_s = int(float(row["t_s"]))
            rows.append(t_s)
        if rows:
            rows.sort()
            points = len(rows)
            first_t_s = rows[0]
            last_t_s = rows[-1]
            if len(rows) >= 2:
                step_s = max(1, rows[1] - rows[0])

manifest = {}
if manifest_path.exists():
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

manifest.update(
    {
        "run_id": run_id,
        "scenario_id": scenario_id,
        "status": status,
        "schedule_path": str(schedule_path),
        "schedule_file_name": schedule_path.name,
        "schedule_step_seconds": step_s,
        "schedule_points": points,
        "schedule_first_t_s": first_t_s,
        "schedule_last_t_s": last_t_s,
        "requested_run_time": run_time,
        "workers": workers,
        "profile": profile,
    }
)
manifest.update(updates)

manifest_path.parent.mkdir(parents=True, exist_ok=True)
manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
PY
}

# Use the minimal schedule-only locustfile by default.
export LOCUSTFILE="${LOCUSTFILE:-$ROOT_DIR/benchmarks/online_boutique/loadgen-locust/locustfile.py}"

export OB_ENABLE_DYNAMIC_SHAPE=1
export OB_DYNAMIC_SHAPE="schedule_file"
export OB_SHAPE_FILE="$(
  python - "$SCHEDULE_FILE" <<'PY'
import os
import sys
from pathlib import Path
print(str(Path(sys.argv[1]).expanduser().resolve()))
PY
)"
export OB_SHAPE_STEP_S="${OB_SHAPE_STEP_S:-10}"
export OB_SHAPE_SPAWN_RATE="${OB_SHAPE_SPAWN_RATE:-300}"

# Ensure the shape stops at RUN_TIME, even if the shell environment
# already has an old OB_SHAPE_DURATION_S exported.
export OB_SHAPE_DURATION_S="$(
  python - "$RUN_TIME" <<'PY'
import re
import sys
value = sys.argv[1].strip().lower()
match = re.fullmatch(r'(\d+)([smhd])', value)
if not match:
    # Default 14 days.
    print(14 * 86400)
    raise SystemExit(0)
amount = int(match.group(1))
unit = match.group(2)
scale = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}[unit]
print(amount * scale)
PY
)"

# Locust itself does not accept the `d` suffix for --run-time, so normalize
# day-based inputs to hours before passing them down.
NORMALIZED_RUN_TIME="$(
  python - "$RUN_TIME" <<'PY'
import re
import sys

value = sys.argv[1].strip().lower()
match = re.fullmatch(r'(\d+)d', value)
if match:
    print(f"{int(match.group(1)) * 24}h")
else:
    print(sys.argv[1])
PY
)"

# Make log dir deterministic per invocation (avoid inheriting old env state).
if [[ "${OB_KEEP_LOCUST_LOG_DIR:-0}" == "1" ]]; then
  : "${LOCUST_LOG_DIR:?OB_KEEP_LOCUST_LOG_DIR=1 requires LOCUST_LOG_DIR to be set}"
else
  export LOCUST_LOG_DIR="$(mktemp -d /tmp/ob-locust-schedule.XXXXXX)"
fi

if [[ -n "$RUN_ARTIFACT_DIR" ]]; then
  export OB_RUN_ARTIFACT_DIR="$(
    python - "$RUN_ARTIFACT_DIR" <<'PY'
import sys
from pathlib import Path
print(str(Path(sys.argv[1]).expanduser().resolve()))
PY
  )"
  mkdir -p "$OB_RUN_ARTIFACT_DIR"
  export OB_RUN_MANIFEST_PATH="$OB_RUN_ARTIFACT_DIR/run_manifest.json"
  export OB_INJECTION_EVENTS_FILE="$OB_RUN_ARTIFACT_DIR/injection_events.jsonl"
fi

export OB_RUN_ID="$RUN_ID"
export OB_PROFILE="$PROFILE"
export OB_SCENARIO_ID="$SCENARIO_ID"

start_requested_utc="$(timestamp_utc)"
if [[ -n "${OB_RUN_MANIFEST_PATH:-}" ]]; then
  update_run_manifest "starting" "{\"launch_requested_utc\": \"$start_requested_utc\"}"
fi
append_injection_event \
  "launch_requested" \
  "$start_requested_utc" \
  "schedule_file" "$OB_SHAPE_FILE" \
  "workers" "$WORKERS" \
  "run_time" "$RUN_TIME"

cleanup_run_record() {
  exit_code=$?
  finish_utc="$(timestamp_utc)"
  final_status="completed"
  if [[ $exit_code -ne 0 ]]; then
    final_status="failed"
  fi
  if [[ -n "${OB_RUN_MANIFEST_PATH:-}" ]]; then
    update_run_manifest "$final_status" "{\"process_exit_utc\": \"$finish_utc\", \"exit_code\": $exit_code}"
  fi
  append_injection_event "process_exit" "$finish_utc" "exit_code" "$exit_code"
}
trap cleanup_run_record EXIT

"$ROOT_DIR/benchmarks/online_boutique/loadgen-locust/run_distributed.sh" \
  "$WORKERS" 1 1 "$NORMALIZED_RUN_TIME" "$@"

