#!/usr/bin/env python3

from __future__ import annotations

import argparse
import csv
import re
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path


TIMESTAMP_RE = re.compile(r"\[(\d{2}/[A-Za-z]{3}/\d{4}:\d{2}:\d{2}:\d{2} [+-]\d{4})\]")
TIMESTAMP_FMT = "%d/%b/%Y:%H:%M:%S %z"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = Path("dataset/raw/NASA-HTTP/NASA_access_log_Aug95")
DEFAULT_OUTPUT = Path("dataset/processed/traffic/NASA-HTTP/NASA_access_log_Aug95_19950807_19950820_weekdays_requests_per_second.csv")
DEFAULT_START_DATE = "1995-08-07"
DEFAULT_END_DATE = "1995-08-20"


def parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"true", "1", "yes", "y"}:
        return True
    if normalized in {"false", "0", "no", "n"}:
        return False
    raise argparse.ArgumentTypeError(f"invalid boolean value: {value!r}; expected true/false")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract continuous per-second request count series from NASA HTTP access logs."
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=DEFAULT_INPUT,
        help="Path to a NASA log file, relative to the project root by default.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Path to an output CSV file, relative to the project root by default.",
    )
    parser.add_argument(
        "--start-date",
        default=DEFAULT_START_DATE,
        help="Inclusive start date in YYYY-MM-DD format.",
    )
    parser.add_argument(
        "--end-date",
        default=DEFAULT_END_DATE,
        help="Inclusive end date in YYYY-MM-DD format.",
    )
    parser.add_argument(
        "--include-weekends",
        type=parse_bool,
        default=False,
        metavar="BOOL",
        help="Whether to include Saturday/Sunday data (true/false). Default: false.",
    )
    return parser.parse_args()


def resolve_project_path(path: Path) -> Path:
    return path if path.is_absolute() else PROJECT_ROOT / path


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def include_date(current_date: date, *, include_weekends: bool) -> bool:
    return include_weekends or current_date.weekday() < 5


def extract_counts(
    input_path: Path,
    start_date: date,
    end_date: date,
    *,
    include_weekends: bool,
) -> tuple[Counter, datetime, datetime, int]:
    counts: Counter = Counter()
    min_ts: datetime | None = None
    max_ts: datetime | None = None
    skipped_lines = 0

    with input_path.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            match = TIMESTAMP_RE.search(line)
            if match is None:
                skipped_lines += 1
                continue

            try:
                ts = datetime.strptime(match.group(1), TIMESTAMP_FMT)
            except ValueError:
                skipped_lines += 1
                continue

            current_date = ts.date()
            if current_date < start_date or current_date > end_date:
                continue
            if not include_date(current_date, include_weekends=include_weekends):
                continue

            second_ts = ts.replace(microsecond=0)
            counts[second_ts] += 1

            if min_ts is None or second_ts < min_ts:
                min_ts = second_ts
            if max_ts is None or second_ts > max_ts:
                max_ts = second_ts

    if min_ts is None or max_ts is None:
        raise ValueError(f"No valid timestamps found in {input_path} for {start_date} to {end_date}")

    return counts, min_ts, max_ts, skipped_lines


def write_series(
    output_path: Path,
    counts: Counter,
    min_ts: datetime,
    max_ts: datetime,
    *,
    include_weekends: bool,
) -> int:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    current = min_ts
    rows_written = 0
    with output_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "request_count"])

        while current <= max_ts:
            if include_date(current.date(), include_weekends=include_weekends):
                writer.writerow([current.isoformat(), counts.get(current, 0)])
                rows_written += 1
            current += timedelta(seconds=1)
    return rows_written

def main() -> None:
    args = parse_args()
    input_path = resolve_project_path(args.input)
    output_path = resolve_project_path(args.output)
    start_date = parse_date(args.start_date)
    end_date = parse_date(args.end_date)

    if end_date < start_date:
        raise ValueError("end-date must be greater than or equal to start-date")

    counts, min_ts, max_ts, skipped_lines = extract_counts(
        input_path,
        start_date,
        end_date,
        include_weekends=args.include_weekends,
    )
    rows_written = write_series(output_path, counts, min_ts, max_ts, include_weekends=args.include_weekends)

    span_seconds = int((max_ts - min_ts).total_seconds()) + 1
    print(f"input={input_path}")
    print(f"output={output_path}")
    print(f"include_weekends={args.include_weekends}")
    print(f"start={min_ts.isoformat()}")
    print(f"end={max_ts.isoformat()}")
    print(f"span_seconds={span_seconds}")
    print(f"written_seconds={rows_written}")
    print(f"nonzero_seconds={len(counts)}")
    print(f"skipped_lines={skipped_lines}")


if __name__ == "__main__":
    main()
