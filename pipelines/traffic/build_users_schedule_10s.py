#!/usr/bin/env python3

import argparse
import csv
import json
from pathlib import Path


def default_schedule_dir(repo_root: Path, dataset_dir_name: str) -> Path:
    return repo_root / "dataset" / "processed" / "traffic" / dataset_dir_name / "schedules"


def percentile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        raise ValueError("empty values")
    if q <= 0:
        return float(sorted_vals[0])
    if q >= 1:
        return float(sorted_vals[-1])
    k = (len(sorted_vals) - 1) * q
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if c == f:
        return float(sorted_vals[f])
    d = k - f
    return float(sorted_vals[f] * (1 - d) + sorted_vals[c] * d)


def load_counts(input_csv: Path, *, step_s: int) -> tuple[dict[int, int], list[float]]:
    if step_s <= 0:
        raise ValueError("step_s must be positive")

    step_counts: dict[int, int] = {}
    minute_counts: dict[int, int] = {}
    logical_second = 0

    with input_csv.open("r", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            count = int(row["request_count"])
            # Treat the extracted CSV as a dense logical time series so skipped weekends
            # do not create two-day zero gaps inside the generated Locust schedule.
            step_bucket = (logical_second // step_s) * step_s
            minute_bucket = (logical_second // 60) * 60
            step_counts[step_bucket] = step_counts.get(step_bucket, 0) + count
            minute_counts[minute_bucket] = minute_counts.get(minute_bucket, 0) + count
            logical_second += 1

    if logical_second == 0:
        raise ValueError(f"No rows in {input_csv}")

    minute_avg_rps_sorted = sorted([v / 60.0 for v in minute_counts.values()])
    return step_counts, minute_avg_rps_sorted


def build_one(
    *,
    input_csv: Path,
    out_csv: Path,
    meta_out: Path | None,
    step_s: int,
    peak_users: int,
) -> dict:
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    if meta_out is not None:
        meta_out.parent.mkdir(parents=True, exist_ok=True)

    step_counts, minute_avg_rps_sorted = load_counts(input_csv, step_s=step_s)
    peak_rps = max((count / float(step_s) for count in step_counts.values()), default=0.0)
    if peak_rps <= 0:
        raise ValueError(f"Non-positive peak rps: {peak_rps}")

    max_step = max(step_counts.keys()) if step_counts else 0
    with out_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["t_s", "users", "req_count", "rps_avg"])
        for t_s in range(0, max_step + 1, step_s):
            req_count = step_counts.get(t_s, 0)
            rps = req_count / float(step_s)
            users = max(0, int(round(peak_users * (rps / peak_rps))))
            w.writerow([t_s, users, req_count, f"{rps:.6f}"])

    meta = {
        "input": str(input_csv),
        "out": str(out_csv),
        "step_s": step_s,
        "peak_users": peak_users,
        "peak_rps": peak_rps,
    }

    if meta_out is not None:
        meta["minute_avg_rps"] = {
            "count": len(minute_avg_rps_sorted),
            "p50": percentile(minute_avg_rps_sorted, 0.50),
            "p90": percentile(minute_avg_rps_sorted, 0.90),
            "p95": percentile(minute_avg_rps_sorted, 0.95),
            "p99": percentile(minute_avg_rps_sorted, 0.99),
            "max": minute_avg_rps_sorted[-1],
        }
        meta_out.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    return meta


def main() -> int:
    p = argparse.ArgumentParser(
        description="Build 10s Locust users schedules from extracted per-second CSVs."
    )
    p.add_argument(
        "--data",
        default="all",
        choices=["all", "clarknet", "nasa"],
        help="Dataset name (default: all).",
    )
    p.add_argument("--step-s", type=int, default=10, help="Step seconds for schedule (default: 10).")
    p.add_argument(
        "--peak-users",
        type=int,
        default=1000,
        help="Target users at the maximum 10s RPS point (default: 1000).",
    )
    p.add_argument(
        "--out-dir",
        default="",
        help="Output directory. Default: dataset/processed/traffic/<dataset>/schedules.",
    )
    p.add_argument(
        "--no-meta",
        action="store_true",
        help="Do not write meta JSON (faster, less verbose).",
    )
    args = p.parse_args()

    repo_root = Path(__file__).resolve().parents[2]
    step_s = int(args.step_s)
    peak_users = int(args.peak_users)

    out_dir = Path(args.out_dir) if args.out_dir else None
    meta_enabled = not bool(args.no_meta)

    datasets = {
        "clarknet": {
            "input": repo_root
            / "dataset/processed/traffic/ClarkNet-HTTP/clarknet_access_log_aug28_sep10_weekdays_requests_per_second.csv",
            "schedule": (out_dir or default_schedule_dir(repo_root, "ClarkNet-HTTP"))
            / f"clarknet_users_10s_peak_u{peak_users}.csv",
            "meta": (out_dir or default_schedule_dir(repo_root, "ClarkNet-HTTP"))
            / f"clarknet_meta_peak_u{peak_users}.json",
        },
        "nasa": {
            "input": repo_root
            / "dataset/processed/traffic/NASA-HTTP/NASA_access_log_Aug95_19950807_19950820_weekdays_requests_per_second.csv",
            "schedule": (out_dir or default_schedule_dir(repo_root, "NASA-HTTP"))
            / f"nasa_users_10s_peak_u{peak_users}.csv",
            "meta": (out_dir or default_schedule_dir(repo_root, "NASA-HTTP"))
            / f"nasa_meta_peak_u{peak_users}.json",
        },
    }

    targets = ["clarknet", "nasa"] if args.data == "all" else [args.data]

    for name in targets:
        conf = datasets[name]
        input_csv: Path = conf["input"]
        out_csv: Path = conf["schedule"]
        meta_out: Path | None = conf["meta"] if meta_enabled else None

        meta = build_one(
            input_csv=input_csv,
            out_csv=out_csv,
            meta_out=meta_out,
            step_s=step_s,
            peak_users=peak_users,
        )

        print(f"[{name}] input : {input_csv}")
        print(f"[{name}] output: {out_csv}")
        if meta_out is not None:
            print(f"[{name}] meta  : {meta_out}")
        print(f"[{name}] peak  : rps={meta['peak_rps']:.6f} -> users={meta['peak_users']}")
        print(f"[{name}] next  : bash benchmarks/online_boutique/loadgen-locust/run_traffic_schedule_10s.sh \"{out_csv}\" 16 30m --web-port 0")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

