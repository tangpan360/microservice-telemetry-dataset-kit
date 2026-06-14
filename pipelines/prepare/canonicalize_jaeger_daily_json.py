#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
from pathlib import Path


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def trace_start_time(trace: dict) -> int:
    spans = trace.get("spans", []) or []
    if not spans:
        return 0
    return min(int(span.get("startTime", 0)) for span in spans)


def canonicalize_day_service(service_day_dir: Path, out_root: Path) -> dict:
    trace_files = sorted(service_day_dir.glob("*/traces.json"))
    service_name = service_day_dir.name
    day_name = service_day_dir.parent.name

    trace_map: dict[str, dict] = {}
    span_map: dict[tuple[str, str], dict] = {}
    source_windows_by_trace: dict[str, set[str]] = {}

    total_trace_rows = 0
    total_span_rows = 0

    for trace_file in trace_files:
        payload = load_json(trace_file)
        window_name = trace_file.parent.name
        for trace in payload.get("data", []) or []:
            trace_id = trace.get("traceID")
            if trace_id is None:
                continue
            total_trace_rows += 1
            source_windows_by_trace.setdefault(trace_id, set()).add(window_name)

            base = trace_map.get(trace_id)
            if base is None:
                base = {
                    "traceID": trace_id,
                    "spans": [],
                    "processes": dict(trace.get("processes") or {}),
                    "warnings": list(trace.get("warnings") or []),
                }
                trace_map[trace_id] = base
            else:
                base["processes"].update(trace.get("processes") or {})
                for warning in trace.get("warnings") or []:
                    if warning not in base["warnings"]:
                        base["warnings"].append(warning)

            for span in trace.get("spans", []) or []:
                span_id = span.get("spanID")
                if span_id is None:
                    continue
                total_span_rows += 1
                span_key = (trace_id, span_id)
                if span_key not in span_map:
                    span_map[span_key] = span

    spans_by_trace: dict[str, list[dict]] = {}
    for (trace_id, _span_id), span in span_map.items():
        spans_by_trace.setdefault(trace_id, []).append(span)

    merged_traces = []
    for trace_id, trace in trace_map.items():
        spans = sorted(
            spans_by_trace.get(trace_id, []),
            key=lambda span: (int(span.get("startTime", 0)), span.get("spanID", "")),
        )
        trace["spans"] = spans
        merged_traces.append(trace)

    merged_traces.sort(key=trace_start_time)

    merged_payload = {
        "data": merged_traces,
        "total": len(merged_traces),
        "limit": len(merged_traces),
        "offset": 0,
        "errors": None,
    }

    out_dir = out_root / day_name / service_name
    out_dir.mkdir(parents=True, exist_ok=True)
    traces_out = out_dir / "traces.json"
    traces_out.write_text(
        json.dumps(merged_payload, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    duplicate_trace_rows = total_trace_rows - len(merged_traces)
    duplicate_span_rows = total_span_rows - len(span_map)
    summary = {
        "day": day_name,
        "service": service_name,
        "source_windows": [p.parent.name for p in trace_files],
        "source_trace_rows": total_trace_rows,
        "unique_trace_ids": len(merged_traces),
        "removed_duplicate_trace_rows": duplicate_trace_rows,
        "source_span_rows": total_span_rows,
        "unique_span_keys": len(span_map),
        "removed_duplicate_span_rows": duplicate_span_rows,
        "output_path": str(traces_out),
        "duplicate_trace_examples": [
            {"trace_id": trace_id, "windows": sorted(windows)}
            for trace_id, windows in source_windows_by_trace.items()
            if len(windows) > 1
        ][:20],
    }
    summary_path = out_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Merge overlapping Jaeger raw exports into one JSON per day."
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
    input_root = dataset_root / "exported" / f"run_id={run_id}" / "jaeger"
    out_root = dataset_root / "canonical" / f"run_id={run_id}" / "jaeger"

    if not input_root.exists():
        raise SystemExit(f"Input root not found: {input_root}")

    out_root.mkdir(parents=True, exist_ok=True)

    all_summaries = []
    day_dirs = sorted(p for p in input_root.iterdir() if p.is_dir())
    for day_dir in day_dirs:
        for service_dir in sorted(p for p in day_dir.iterdir() if p.is_dir()):
            summary = canonicalize_day_service(service_dir, out_root)
            all_summaries.append(summary)
            print(f"merged jaeger day {day_dir.name} service {service_dir.name} -> {out_root / day_dir.name / service_dir.name}")

    index_path = out_root / "index.json"
    index_path.write_text(json.dumps(all_summaries, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
