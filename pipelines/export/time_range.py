from __future__ import annotations

import time
from datetime import datetime, timezone


def parse_time_arg(value: str) -> int:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(
            f"Invalid time {value!r}. Use RFC3339 with timezone, e.g. 2026-04-22T14:00:00+08:00."
        ) from exc

    if dt.tzinfo is None:
        raise ValueError(
            f"Time {value!r} is missing timezone. Use RFC3339 with timezone, e.g. 2026-04-22T06:00:00Z."
        )

    return int(dt.timestamp())


def to_rfc3339_utc(epoch_s: int) -> str:
    return datetime.fromtimestamp(epoch_s, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def resolve_window(*, minutes: int | None, hours: int | None, days: int | None) -> tuple[int, str, int]:
    choices = []
    if minutes is not None:
        choices.append(("minutes", minutes, minutes * 60))
    if hours is not None:
        choices.append(("hours", hours, hours * 3600))
    if days is not None:
        choices.append(("days", days, days * 86400))

    if not choices:
        return 30 * 60, "minutes", 30

    if len(choices) > 1:
        raise ValueError("Use only one of --minutes, --hours, or --days.")

    unit, value, seconds = choices[0]
    if value <= 0:
        raise ValueError(f"--{unit} must be > 0.")

    return seconds, unit, value


def resolve_time_range(
    *,
    start: str,
    end: str,
    minutes: int | None,
    hours: int | None,
    days: int | None,
) -> tuple[int, int, str, str, int]:
    window_s, window_unit, window_value = resolve_window(
        minutes=minutes,
        hours=hours,
        days=days,
    )

    if start and end:
        start_s = parse_time_arg(start)
        end_s = parse_time_arg(end)
        mode = "start_end"
    elif start:
        start_s = parse_time_arg(start)
        end_s = start_s + window_s
        mode = f"start_plus_{window_unit}"
    elif end:
        end_s = parse_time_arg(end)
        start_s = end_s - window_s
        mode = f"end_minus_{window_unit}"
    else:
        end_s = int(time.time())
        start_s = end_s - window_s
        mode = f"last_{window_unit}"

    if start_s >= end_s:
        raise ValueError("Time range is invalid: start must be earlier than end.")

    return start_s, end_s, mode, window_unit, window_value
