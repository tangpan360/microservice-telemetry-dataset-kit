#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path

import pandas as pd


PROM_METRIC_SPECS = {
    "service_in_rps.json": ("destination_workload", "service_in_rps"),
    "service_out_rps.json": ("source_workload", "service_out_rps"),
    "service_latency_mean_ms.json": ("destination_workload", "service_latency_mean_ms"),
    "service_latency_p95_ms.json": ("destination_workload", "service_latency_p95_ms"),
    "service_latency_p99_ms.json": ("destination_workload", "service_latency_p99_ms"),
    "service_error_rate.json": ("destination_workload", "service_error_rate"),
    "service_timeout_rate.json": ("destination_workload", "service_timeout_rate"),
    "cpu_cores.json": ("deployment", "cpu_cores"),
    "memory_working_set_bytes.json": ("deployment", "memory_working_set_bytes"),
    "replicas_spec.json": ("deployment", "replica_count"),
    "replicas_available.json": ("deployment", "ready_replica_count"),
    "cpu_throttling_ratio.json": ("deployment", "cpu_throttling_ratio"),
    "active_node_count.json": ("deployment", "active_node_count"),
}


DEFAULT_EXCLUDED_SERVICES = {
    "istio-ingressgateway.istio-system",
    "loadgenerator",
    "loadgenerator.ob",
    "jaeger-all-in-one",
}


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def minute_bucket_s(timestamp_s: int | float) -> int:
    return int(float(timestamp_s) // 60 * 60)


def ceil_utc_to_minute_bucket_s(value: str) -> int:
    ts = pd.to_datetime(value, utc=True)
    return int(ts.ceil("min").timestamp())


def normalize_service_name(name: str) -> str:
    if not name:
        return ""
    if name.endswith(".ob"):
        return name[: -len(".ob")]
    return name


def quantile_p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = 0.95 * (len(ordered) - 1)
    low = math.floor(pos)
    high = math.ceil(pos)
    if low == high:
        return ordered[low]
    frac = pos - low
    return ordered[low] * (1 - frac) + ordered[high] * frac


def build_prom_rows(prom_day_dir: Path, excluded_services: set[str]) -> list[dict]:
    rows_by_key: dict[tuple[int, str], dict] = {}
    for file_name, (label_key, output_col) in PROM_METRIC_SPECS.items():
        path = prom_day_dir / file_name
        if not path.exists():
            continue
        payload = load_json(path)
        for series in payload.get("data", {}).get("result", []):
            raw_service = series.get("metric", {}).get(label_key, "")
            service = normalize_service_name(raw_service)
            if not service or raw_service in excluded_services or service in excluded_services:
                continue
            for ts, value in series.get("values", []):
                bucket_s = minute_bucket_s(ts)
                key = (bucket_s, service)
                row = rows_by_key.setdefault(
                    key,
                    {
                        "timestamp_bucket_s": bucket_s,
                        "service": service,
                    },
                )
                row[output_col] = float(value)

    rows = []
    for row in rows_by_key.values():
        bucket_s = int(row["timestamp_bucket_s"])
        dt = pd.to_datetime(bucket_s, unit="s", utc=True)
        row["timestamp_bucket_utc"] = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        row["hour_of_day"] = int(dt.hour)
        row["day_of_week"] = int(dt.dayofweek)
        row["minute_of_day"] = int(dt.hour * 60 + dt.minute)
        row["hour_sin"] = math.sin(2 * math.pi * dt.hour / 24.0)
        row["hour_cos"] = math.cos(2 * math.pi * dt.hour / 24.0)
        row["dow_sin"] = math.sin(2 * math.pi * dt.dayofweek / 7.0)
        row["dow_cos"] = math.cos(2 * math.pi * dt.dayofweek / 7.0)
        row["is_weekend"] = int(dt.dayofweek >= 5)
        rows.append(row)
    return rows


def span_error(span: dict) -> int:
    for tag in span.get("tags", []) or []:
        key = tag.get("key")
        value = str(tag.get("value", ""))
        if key == "http.status_code":
            try:
                return int(value) >= 500
            except ValueError:
                return 0
        if key == "response_flags":
            return value not in {"", "-"}
    return 0


def build_edge_rows(jaeger_day_service_dir: Path, excluded_services: set[str]) -> list[dict]:
    traces_path = jaeger_day_service_dir / "traces.json"
    if not traces_path.exists():
        return []

    payload = load_json(traces_path)
    edge_values: dict[tuple[int, str, str], dict] = {}

    for trace in payload.get("data", []) or []:
        processes = trace.get("processes") or {}
        spans = trace.get("spans", []) or []
        spans_by_id = {span.get("spanID"): span for span in spans if span.get("spanID")}
        for span in spans:
            span_id = span.get("spanID")
            if not span_id:
                continue
            process_id = span.get("processID", "")
            callee_raw = (processes.get(process_id) or {}).get("serviceName", "")
            callee = normalize_service_name(callee_raw)
            if not callee or callee_raw in excluded_services or callee in excluded_services:
                continue

            parent_span = None
            for ref in span.get("references", []) or []:
                if ref.get("refType") == "CHILD_OF":
                    parent_span = spans_by_id.get(ref.get("spanID"))
                    if parent_span is not None:
                        break
            if parent_span is None:
                continue

            parent_process_id = parent_span.get("processID", "")
            caller_raw = (processes.get(parent_process_id) or {}).get("serviceName", "")
            caller = normalize_service_name(caller_raw)
            if not caller or caller_raw in excluded_services or caller in excluded_services:
                continue
            if caller == callee:
                continue

            bucket_s = minute_bucket_s(int(span.get("startTime", 0)) / 1_000_000)
            key = (bucket_s, caller, callee)
            bucket = edge_values.setdefault(
                key,
                {
                    "timestamp_bucket_s": bucket_s,
                    "caller_service": caller,
                    "callee_service": callee,
                    "latencies_ms": [],
                    "error_count": 0,
                },
            )
            bucket["latencies_ms"].append(float(span.get("duration", 0)) / 1000.0)
            bucket["error_count"] += int(span_error(span))

    rows = []
    for bucket in edge_values.values():
        latencies = bucket.pop("latencies_ms")
        bucket["call_count"] = len(latencies)
        bucket["latency_mean_ms"] = float(sum(latencies) / len(latencies)) if latencies else 0.0
        bucket["latency_p95_ms"] = quantile_p95(latencies)
        bucket["latency_max_ms"] = max(latencies) if latencies else 0.0
        dt = pd.to_datetime(bucket["timestamp_bucket_s"], unit="s", utc=True)
        bucket["timestamp_bucket_utc"] = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        rows.append(bucket)
    return rows


def attach_dependency_features(service_df: pd.DataFrame, edge_df: pd.DataFrame) -> pd.DataFrame:
    feature_cols = [
        "upstream_total_calls",
        "upstream_service_count",
        "upstream_latency_mean_ms",
        "upstream_latency_max_ms",
        "downstream_total_calls",
        "downstream_service_count",
        "downstream_latency_mean_ms",
        "downstream_latency_max_ms",
        "top1_upstream_calls",
        "top2_upstream_calls",
        "top3_upstream_calls",
        "top1_downstream_calls",
        "top2_downstream_calls",
        "top3_downstream_calls",
    ]
    for col in feature_cols:
        service_df[col] = 0.0

    if edge_df.empty:
        return service_df

    edge_records = edge_df.to_dict("records")
    upstream_map: dict[tuple[int, str], list[dict]] = defaultdict(list)
    downstream_map: dict[tuple[int, str], list[dict]] = defaultdict(list)
    for row in edge_records:
        upstream_map[(row["timestamp_bucket_s"], row["callee_service"])].append(row)
        downstream_map[(row["timestamp_bucket_s"], row["caller_service"])].append(row)

    def enrich(rows: list[dict], prefix: str) -> dict[str, float]:
        if not rows:
            return {}
        total_calls = sum(int(row["call_count"]) for row in rows)
        weighted_latency = (
            sum(float(row["latency_mean_ms"]) * int(row["call_count"]) for row in rows) / total_calls
            if total_calls
            else 0.0
        )
        max_latency = max(float(row["latency_max_ms"]) for row in rows)
        counts = sorted((int(row["call_count"]) for row in rows), reverse=True)
        return {
            f"{prefix}_total_calls": float(total_calls),
            f"{prefix}_service_count": float(len(rows)),
            f"{prefix}_latency_mean_ms": float(weighted_latency),
            f"{prefix}_latency_max_ms": float(max_latency),
            f"top1_{prefix}_calls": float(counts[0]) if len(counts) > 0 else 0.0,
            f"top2_{prefix}_calls": float(counts[1]) if len(counts) > 1 else 0.0,
            f"top3_{prefix}_calls": float(counts[2]) if len(counts) > 2 else 0.0,
        }

    for idx, row in service_df.iterrows():
        key = (int(row["timestamp_bucket_s"]), row["service"])
        for col, value in enrich(upstream_map.get(key, []), "upstream").items():
            service_df.at[idx, col] = value
        for col, value in enrich(downstream_map.get(key, []), "downstream").items():
            service_df.at[idx, col] = value
    return service_df


def build_run_metadata(
    *,
    day: str,
    run_id: str,
    scenario_id: str,
    service_df: pd.DataFrame,
) -> pd.DataFrame:
    if service_df.empty:
        return pd.DataFrame(
            [
                {
                    "run_id": run_id,
                    "scenario_id": scenario_id,
                    "app_name": "online_boutique",
                    "start_time_utc": "",
                    "end_time_utc": "",
                    "bucket_seconds": 60,
                    "service_count": 0,
                    "day": day,
                }
            ]
        )

    return pd.DataFrame(
        [
            {
                "run_id": run_id,
                "scenario_id": scenario_id,
                "app_name": "online_boutique",
                "start_time_utc": service_df["timestamp_bucket_utc"].min(),
                "end_time_utc": service_df["timestamp_bucket_utc"].max(),
                "bucket_seconds": 60,
                "service_count": int(service_df["service"].nunique()),
                "day": day,
            }
        ]
    )


def resolve_scenario_id(dataset_root: Path, run_id: str, scenario_id_override: str) -> str:
    if scenario_id_override.strip():
        return scenario_id_override.strip()
    manifest_path = dataset_root / "exported" / f"run_id={run_id}" / "run_manifest.json"
    if not manifest_path.exists():
        return ""
    return str(load_json(manifest_path).get("scenario_id", "")).strip()


def resolve_effective_bounds(dataset_root: Path, run_id: str) -> tuple[int, int]:
    manifest_path = dataset_root / "exported" / f"run_id={run_id}" / "run_manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"run_manifest.json not found: {manifest_path}")

    manifest = load_json(manifest_path)
    inject_start_utc = str(manifest.get("inject_start_utc", "")).strip()
    inject_end_utc = str(manifest.get("inject_end_utc", "")).strip()
    if not inject_start_utc or not inject_end_utc:
        raise ValueError(
            f"run_manifest.json missing inject_start_utc or inject_end_utc: {manifest_path}"
        )

    # Drop the first and last effective minute buckets as requested:
    # start one minute later, end one minute earlier.
    start_bucket_s = ceil_utc_to_minute_bucket_s(inject_start_utc) + 60
    end_bucket_s = ceil_utc_to_minute_bucket_s(inject_end_utc) - 60
    if end_bucket_s < start_bucket_s:
        raise ValueError(
            f"Resolved invalid effective bounds: start={inject_start_utc}, end={inject_end_utc}"
        )
    return start_bucket_s, end_bucket_s


def filter_by_effective_bounds(
    df: pd.DataFrame,
    *,
    start_bucket_s: int,
    end_bucket_s: int,
) -> pd.DataFrame:
    if df.empty:
        return df
    mask = (df["timestamp_bucket_s"] >= start_bucket_s) & (df["timestamp_bucket_s"] <= end_bucket_s)
    return df.loc[mask].reset_index(drop=True)


def deduplicate_service_df(service_df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    if service_df.empty:
        return service_df, 0
    before = len(service_df)
    service_df = (
        service_df.sort_values(["timestamp_bucket_s", "service", "day"])
        .drop_duplicates(subset=["timestamp_bucket_utc", "service"], keep="first")
        .reset_index(drop=True)
    )
    return service_df, before - len(service_df)


def deduplicate_edge_df(edge_df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    if edge_df.empty:
        return edge_df, 0
    before = len(edge_df)
    edge_df = (
        edge_df.sort_values(["timestamp_bucket_s", "caller_service", "callee_service", "day"])
        .drop_duplicates(
            subset=["timestamp_bucket_utc", "caller_service", "callee_service"],
            keep="first",
        )
        .reset_index(drop=True)
    )
    return edge_df, before - len(edge_df)


def build_service_metadata(service_df: pd.DataFrame, service_idx_map: dict[str, int]) -> pd.DataFrame:
    if service_df.empty:
        return pd.DataFrame(columns=["service", "service_idx", "first_seen_timestamp_bucket_s", "last_seen_timestamp_bucket_s", "total_active_minutes"])

    grouped = (
        service_df.groupby("service", as_index=False)
        .agg(
            first_seen_timestamp_bucket_s=("timestamp_bucket_s", "min"),
            last_seen_timestamp_bucket_s=("timestamp_bucket_s", "max"),
            total_active_minutes=("timestamp_bucket_s", "nunique"),
        )
        .sort_values("service")
        .reset_index(drop=True)
    )
    grouped["service_idx"] = grouped["service"].map(service_idx_map).astype(int)
    return grouped[["service", "service_idx", "first_seen_timestamp_bucket_s", "last_seen_timestamp_bucket_s", "total_active_minutes"]]


def write_csv(df: pd.DataFrame, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False)


def write_table(df: pd.DataFrame, base_path: Path) -> None:
    write_csv(df, base_path.with_suffix(".csv"))


def build_ready_summary(
    *,
    run_id: str,
    scenario_id: str,
    service_df: pd.DataFrame,
    edge_df: pd.DataFrame,
    service_duplicates_removed: int,
    edge_duplicates_removed: int,
) -> dict:
    return {
        "run_id": run_id,
        "scenario_id": scenario_id,
        "service_rows": int(len(service_df)),
        "edge_rows": int(len(edge_df)),
        "service_duplicates_removed": int(service_duplicates_removed),
        "edge_duplicates_removed": int(edge_duplicates_removed),
        "service_count": int(service_df["service"].nunique()) if not service_df.empty else 0,
        "minute_count": int(service_df["timestamp_bucket_utc"].nunique()) if not service_df.empty else 0,
        "start_time_utc": "" if service_df.empty else str(service_df["timestamp_bucket_utc"].min()),
        "end_time_utc": "" if service_df.empty else str(service_df["timestamp_bucket_utc"].max()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build online_boutique tables from canonical JSON."
    )
    parser.add_argument(
        "--dataset-root",
        required=True,
        help="Dataset root, e.g. dataset/processed/online_boutique_clarknet.",
    )
    parser.add_argument(
        "--run-id",
        required=True,
        help="Run identifier under <dataset-root>/canonical and <dataset-root>/tables.",
    )
    parser.add_argument("--scenario-id", default="", help="Optional scenario id override.")
    parser.add_argument(
        "--exclude-services",
        default="istio-ingressgateway.istio-system,loadgenerator,loadgenerator.ob,jaeger-all-in-one",
        help="Comma-separated services to exclude from tables.",
    )
    args = parser.parse_args()

    dataset_root = Path(args.dataset_root).expanduser().resolve()
    run_id = args.run_id.strip()
    prom_root = dataset_root / "canonical" / f"run_id={run_id}" / "prom"
    jaeger_root = dataset_root / "canonical" / f"run_id={run_id}" / "jaeger"
    out_root = dataset_root / "tables" / f"run_id={run_id}"

    excluded_services = {item.strip() for item in args.exclude_services.split(",") if item.strip()}
    excluded_services.update(DEFAULT_EXCLUDED_SERVICES)
    scenario_id = resolve_scenario_id(dataset_root, run_id, args.scenario_id)
    effective_start_bucket_s, effective_end_bucket_s = resolve_effective_bounds(dataset_root, run_id)

    out_root.mkdir(parents=True, exist_ok=True)

    day_dirs = sorted(p for p in prom_root.iterdir() if p.is_dir())
    if not day_dirs:
        raise SystemExit(f"No canonical prom day directories found under: {prom_root}")

    day_results: list[tuple[str, pd.DataFrame, pd.DataFrame]] = []

    for day_dir in day_dirs:
        day = day_dir.name
        prom_rows = build_prom_rows(day_dir, excluded_services)
        service_df = pd.DataFrame(prom_rows)
        if service_df.empty:
            continue

        numeric_defaults = {
            "service_in_rps": 0.0,
            "service_out_rps": 0.0,
            "service_latency_mean_ms": 0.0,
            "service_latency_p95_ms": 0.0,
            "service_latency_p99_ms": 0.0,
            "service_error_rate": 0.0,
            "service_timeout_rate": 0.0,
            "cpu_cores": 0.0,
            "memory_working_set_bytes": 0.0,
            "replica_count": 0.0,
            "ready_replica_count": 0.0,
            "cpu_throttling_ratio": 0.0,
            "active_node_count": 0.0,
        }
        for col, default in numeric_defaults.items():
            if col not in service_df.columns:
                service_df[col] = default
        service_df = service_df.fillna(numeric_defaults)

        jaeger_service_dirs = []
        jaeger_day_dir = jaeger_root / day
        if jaeger_day_dir.exists():
            jaeger_service_dirs = sorted(p for p in jaeger_day_dir.iterdir() if p.is_dir())
        edge_rows = []
        for service_day_dir in jaeger_service_dirs:
            edge_rows.extend(build_edge_rows(service_day_dir, excluded_services))
        edge_df = pd.DataFrame(edge_rows)
        if edge_df.empty:
            edge_df = pd.DataFrame(
                columns=[
                    "timestamp_bucket_s",
                    "timestamp_bucket_utc",
                    "caller_service",
                    "callee_service",
                    "call_count",
                    "latency_mean_ms",
                    "latency_p95_ms",
                    "latency_max_ms",
                    "error_count",
                ]
            )
        else:
            edge_df = edge_df.sort_values(
                ["timestamp_bucket_s", "caller_service", "callee_service"]
            ).reset_index(drop=True)

        service_df = filter_by_effective_bounds(
            service_df,
            start_bucket_s=effective_start_bucket_s,
            end_bucket_s=effective_end_bucket_s,
        )
        edge_df = filter_by_effective_bounds(
            edge_df,
            start_bucket_s=effective_start_bucket_s,
            end_bucket_s=effective_end_bucket_s,
        )
        if service_df.empty:
            continue

        service_df = attach_dependency_features(service_df, edge_df)
        service_df["run_id"] = run_id
        service_df["scenario_id"] = scenario_id
        service_df["day"] = day
        service_df = service_df.sort_values(["timestamp_bucket_s", "service"]).reset_index(drop=True)

        edge_df["run_id"] = run_id
        edge_df["scenario_id"] = scenario_id
        edge_df["day"] = day
        day_results.append((day, service_df, edge_df))

    combined_service_df = (
        pd.concat([service_df for _, service_df, _ in day_results], ignore_index=True)
        if day_results
        else pd.DataFrame()
    )
    combined_edge_df = (
        pd.concat([edge_df for _, _, edge_df in day_results], ignore_index=True)
        if day_results
        else pd.DataFrame()
    )

    combined_service_df, service_duplicates_removed = deduplicate_service_df(combined_service_df)
    combined_edge_df, edge_duplicates_removed = deduplicate_edge_df(combined_edge_df)

    service_order = sorted(combined_service_df["service"].unique()) if not combined_service_df.empty else []
    service_idx_map = {service: idx for idx, service in enumerate(service_order)}

    if not combined_service_df.empty:
        combined_service_df["service_idx"] = combined_service_df["service"].map(service_idx_map).astype(int)
        combined_service_df = combined_service_df.sort_values(
            ["timestamp_bucket_s", "service"]
        ).reset_index(drop=True)

    if not combined_edge_df.empty:
        combined_edge_df["caller_idx"] = (
            combined_edge_df["caller_service"].map(service_idx_map).fillna(-1).astype(int)
        )
        combined_edge_df["callee_idx"] = (
            combined_edge_df["callee_service"].map(service_idx_map).fillna(-1).astype(int)
        )
        combined_edge_df = combined_edge_df.sort_values(
            ["timestamp_bucket_s", "caller_service", "callee_service"]
        ).reset_index(drop=True)

    service_metadata_df = build_service_metadata(combined_service_df, service_idx_map)
    run_metadata_rows = []
    if not combined_service_df.empty:
        for day, group in combined_service_df.groupby("day", sort=True):
            run_metadata_rows.append(
                build_run_metadata(
                    day=day,
                    run_id=run_id,
                    scenario_id=scenario_id,
                    service_df=group.reset_index(drop=True),
                )
            )
    run_metadata_df = (
        pd.concat(run_metadata_rows, ignore_index=True) if run_metadata_rows else pd.DataFrame()
    )
    ready_summary = build_ready_summary(
        run_id=run_id,
        scenario_id=scenario_id,
        service_df=combined_service_df,
        edge_df=combined_edge_df,
        service_duplicates_removed=service_duplicates_removed,
        edge_duplicates_removed=edge_duplicates_removed,
    )

    write_csv(combined_service_df, out_root / "service_minute_features.csv")
    write_csv(combined_edge_df, out_root / "service_call_edge_minute.csv")
    write_csv(service_metadata_df, out_root / "service_metadata.csv")
    write_csv(run_metadata_df, out_root / "run_metadata.csv")
    (out_root / "ready_summary.json").write_text(
        json.dumps(ready_summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
