#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path


def metric_identity(metric: dict) -> str:
    return json.dumps(metric, sort_keys=True, separators=(",", ":"))


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def canonicalize_day(day_dir: Path, out_root: Path) -> dict:
    metric_files: dict[str, list[Path]] = defaultdict(list)
    window_dirs = sorted(p for p in day_dir.iterdir() if p.is_dir())
    for window_dir in window_dirs:
        for path in sorted(window_dir.glob("*.json")):
            if path.name == "meta.json":
                continue
            metric_files[path.name].append(path)

    day_out_dir = out_root / day_dir.name
    day_out_dir.mkdir(parents=True, exist_ok=True)

    summary = {
        "day": day_dir.name,
        "source_windows": [p.name for p in window_dirs],
        "metrics": {},
    }

    for metric_file_name, paths in sorted(metric_files.items()):
        series_values: dict[str, dict[int, str]] = defaultdict(dict)
        series_metric: dict[str, dict] = {}
        total_rows = 0

        for path in sorted(paths):
            payload = load_json(path)
            for series in payload.get("data", {}).get("result", []):
                metric = series.get("metric", {})
                metric_key = metric_identity(metric)
                series_metric[metric_key] = metric
                for ts, value in series.get("values", []):
                    bucket_s = int(float(ts))
                    total_rows += 1
                    series_values[metric_key][bucket_s] = value

        result = []
        unique_rows = 0
        for metric_key in sorted(series_metric.keys()):
            points = sorted(series_values[metric_key].items())
            unique_rows += len(points)
            result.append(
                {
                    "metric": series_metric[metric_key],
                    "values": [[ts, value] for ts, value in points],
                }
            )

        merged_payload = {
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": result,
            },
        }
        out_path = day_out_dir / metric_file_name
        out_path.write_text(
            json.dumps(merged_payload, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

        summary["metrics"][metric_file_name] = {
            "source_file_count": len(paths),
            "source_rows": total_rows,
            "unique_rows": unique_rows,
            "removed_duplicate_rows": total_rows - unique_rows,
            "series_count": len(result),
            "output_path": str(out_path),
        }

    summary_path = day_out_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Merge overlapping Prom raw exports into one JSON per day per metric."
    )
    parser.add_argument(
        "--dataset-root",
        required=True,
        help="Dataset root, e.g. dataset/processed/online_boutique_clarknet.",
    )
    parser.add_argument(
        "--run-id",
        required=True,
        help="Run identifier under <dataset-root>/exported and <dataset-root>/canonical.",
    )
    args = parser.parse_args()

    dataset_root = Path(args.dataset_root).expanduser().resolve()
    run_id = args.run_id.strip()
    input_root = dataset_root / "exported" / f"run_id={run_id}" / "prom"
    out_root = dataset_root / "canonical" / f"run_id={run_id}" / "prom"

    if not input_root.exists():
        raise SystemExit(f"Input root not found: {input_root}")

    out_root.mkdir(parents=True, exist_ok=True)

    day_dirs = sorted(p for p in input_root.iterdir() if p.is_dir())
    if not day_dirs:
        raise SystemExit(f"No day directories found under: {input_root}")

    all_summaries = []
    for day_dir in day_dirs:
        summary = canonicalize_day(day_dir, out_root)
        all_summaries.append(summary)
        print(f"merged prom day {day_dir.name} -> {out_root / day_dir.name}")

    index_path = out_root / "index.json"
    index_path.write_text(json.dumps(all_summaries, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
