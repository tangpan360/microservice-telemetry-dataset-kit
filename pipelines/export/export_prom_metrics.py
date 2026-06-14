#!/usr/bin/env python3

import argparse
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from service_urls import default_prom_url
from time_range import resolve_time_range, to_rfc3339_utc


def http_get_json(url: str, timeout_s: int = 30) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return json.loads(resp.read().decode("utf-8"))


def query_range(
    prom_base: str,
    query: str,
    *,
    start_s: int,
    end_s: int,
    step_s: int,
    timeout_s: int = 30,
) -> dict:
    params = {
        "query": query,
        "start": start_s,
        "end": end_s,
        "step": step_s,
    }
    encoded = urllib.parse.urlencode(params)
    url = f"{prom_base.rstrip('/')}/api/v1/query_range?{encoded}"
    payload = http_get_json(url, timeout_s=timeout_s)
    if payload.get("status") != "success":
        raise RuntimeError(f"Prometheus query failed: {payload}")
    return payload


def build_queries(ns: str, window: str) -> dict[str, str]:
    deploy_re = r"^(.*)-[a-z0-9]{8,10}-[a-z0-9]{5}$"
    label = "destination_workload"

    def zero_fill(expr: str, baseline_expr: str, on_label: str = label) -> str:
        return f"(({expr}) or on({on_label}) (0 * ({baseline_expr})))"

    def safe_div(num_expr: str, den_expr: str, on_label: str = label) -> str:
        return f"({zero_fill(num_expr, den_expr, on_label)}) / clamp_min(({den_expr}), 1e-12)"

    def safe_histogram_quantile(q: float, bucket_expr: str, baseline_expr: str, on_label: str = label) -> str:
        raw_quantile = f"histogram_quantile({q}, {bucket_expr})"
        return (
            f"((({raw_quantile}) unless on({on_label}) (({baseline_expr}) == 0)) "
            f"or on({on_label}) (0 * ({baseline_expr})))"
        )

    req_by_dest = (
        f"sum by ({label}) ("
        f"rate(istio_requests_total{{destination_workload_namespace=\"{ns}\"}}[{window}])"
        f")"
    )
    req_5xx_by_dest = (
        f"sum by ({label}) ("
        f"rate(istio_requests_total{{destination_workload_namespace=\"{ns}\",response_code=~\"5..\"}}[{window}])"
        f")"
    )
    req_timeout_by_dest = (
        f"sum by ({label}) ("
        f"rate(istio_requests_total{{destination_workload_namespace=\"{ns}\",response_flags=~\".*UT.*|.*URX.*|.*UF.*\"}}[{window}])"
        f")"
    )
    latency_sum_by_dest = (
        f"sum by ({label}) ("
        f"rate(istio_request_duration_milliseconds_sum{{destination_workload_namespace=\"{ns}\"}}[{window}])"
        f")"
    )
    latency_count_by_dest = (
        f"sum by ({label}) ("
        f"rate(istio_request_duration_milliseconds_count{{destination_workload_namespace=\"{ns}\"}}[{window}])"
        f")"
    )
    latency_bucket_by_dest = (
        f"sum by (le, {label}) ("
        f"rate(istio_request_duration_milliseconds_bucket{{destination_workload_namespace=\"{ns}\"}}[{window}])"
        f")"
    )

    return {
        "service_in_rps": (
            f"sum by (destination_workload) ("
            f"rate(istio_requests_total{{destination_workload_namespace=\"{ns}\"}}[{window}])"
            f")"
        ),
        "service_out_rps": (
            f"sum by (source_workload) ("
            f"rate(istio_requests_total{{source_workload_namespace=\"{ns}\"}}[{window}])"
            f")"
        ),
        "service_error_5xx_rps": (
            f"sum by (destination_workload) ("
            f"rate(istio_requests_total{{destination_workload_namespace=\"{ns}\",response_code=~\"5..\"}}[{window}])"
            f")"
        ),
        "service_error_rate": safe_div(req_5xx_by_dest, req_by_dest),
        "service_timeout_rate": safe_div(req_timeout_by_dest, req_by_dest),
        "service_latency_mean_ms": safe_div(latency_sum_by_dest, latency_count_by_dest),
        "service_latency_p95_ms": safe_histogram_quantile(0.95, latency_bucket_by_dest, latency_count_by_dest),
        "service_latency_p99_ms": safe_histogram_quantile(0.99, latency_bucket_by_dest, latency_count_by_dest),
        "cpu_cores": (
            "sum by (deployment) ("
            'label_replace('
            f"sum by (pod) (rate(container_cpu_usage_seconds_total{{namespace=\"{ns}\",container!=\"\",image!=\"\"}}[{window}])),"
            f'"deployment","$1","pod","{deploy_re}"'
            ")"
            ")"
        ),
        "memory_working_set_bytes": (
            "sum by (deployment) ("
            'label_replace('
            f"sum by (pod) (container_memory_working_set_bytes{{namespace=\"{ns}\",container!=\"\",image!=\"\"}}),"
            f'"deployment","$1","pod","{deploy_re}"'
            ")"
            ")"
        ),
        "replicas_spec": f'kube_deployment_spec_replicas{{namespace="{ns}"}}',
        "replicas_available": f'kube_deployment_status_replicas_available{{namespace="{ns}"}}',
        "active_node_count": (
            "count by (deployment) ("
            "count by (deployment, node) ("
            'label_replace('
            f"kube_pod_info{{namespace=\"{ns}\"}},"
            f'"deployment","$1","pod","{deploy_re}"'
            ")"
            ")"
            ")"
        ),
        "cpu_throttling_ratio": (
            "("
            "sum by (deployment) ("
            'label_replace('
            f"sum by (pod) (rate(container_cpu_cfs_throttled_periods_total{{namespace=\"{ns}\",container!=\"\",image!=\"\"}}[{window}])),"
            f'"deployment","$1","pod","{deploy_re}"'
            ")"
            ")"
            ") / ("
            "sum by (deployment) ("
            'label_replace('
            f"sum by (pod) (rate(container_cpu_cfs_periods_total{{namespace=\"{ns}\",container!=\"\",image!=\"\"}}[{window}])),"
            f'"deployment","$1","pod","{deploy_re}"'
            ")"
            ")"
            ")"
        ),
    }


def default_out_dir() -> Path:
    repo_root = Path(__file__).resolve().parents[2]
    ts = time.strftime("%Y%m%d_%H%M%S")
    return repo_root / "runs" / "export_prom_metrics" / ts


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export core Online Boutique Prometheus metrics as raw JSON."
    )
    parser.add_argument(
        "--prom",
        default=default_prom_url(),
        help="Prometheus base URL. Default: http://127.0.0.1:19090.",
    )
    parser.add_argument("--namespace", default="ob", help="Kubernetes namespace.")
    parser.add_argument("--window", default="1m", help="Rate window, e.g. 1m/2m/5m.")
    parser.add_argument("--minutes", type=int, default=None, help="Window length in minutes. Default: 30.")
    parser.add_argument("--hours", type=int, default=None, help="Window length in hours.")
    parser.add_argument("--days", type=int, default=None, help="Window length in days.")
    parser.add_argument(
        "--start",
        default="",
        help="Window start time in RFC3339 with timezone, e.g. 2026-04-22T06:00:00Z.",
    )
    parser.add_argument(
        "--end",
        default="",
        help="Window end time in RFC3339 with timezone, e.g. 2026-04-22T14:00:00+08:00.",
    )
    parser.add_argument("--step-s", type=int, default=60, help="query_range step in seconds.")
    parser.add_argument("--run-id", default="", help="Optional experiment run identifier.")
    parser.add_argument("--scenario-id", default="", help="Optional experiment scenario identifier.")
    parser.add_argument(
        "--timeout-s",
        type=int,
        default=60,
        help="HTTP timeout in seconds for each Prometheus query. Default: 60.",
    )
    parser.add_argument("--out-dir", default="", help="Output directory.")
    args = parser.parse_args()

    out_dir = Path(args.out_dir) if args.out_dir else default_out_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        start_s, end_s, time_mode, window_unit, window_value = resolve_time_range(
            start=args.start,
            end=args.end,
            minutes=args.minutes,
            hours=args.hours,
            days=args.days,
        )
    except ValueError as exc:
        raise SystemExit(str(exc))
    if args.step_s <= 0:
        raise SystemExit("--step-s must be > 0.")
    if args.timeout_s <= 0:
        raise SystemExit("--timeout-s must be > 0.")
    queries = build_queries(args.namespace, args.window)

    meta = {
        "prom": args.prom,
        "namespace": args.namespace,
        "run_id": args.run_id,
        "scenario_id": args.scenario_id,
        "window": args.window,
        "minutes": args.minutes,
        "hours": args.hours,
        "days": args.days,
        "window_unit": window_unit,
        "window_value": window_value,
        "time_mode": time_mode,
        "start": args.start,
        "end": args.end,
        "step_s": args.step_s,
        "timeout_s": args.timeout_s,
        "start_s": start_s,
        "end_s": end_s,
        "start_utc": to_rfc3339_utc(start_s),
        "end_utc": to_rfc3339_utc(end_s),
        "queries": queries,
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

    for name, query in queries.items():
        try:
            payload = query_range(
                args.prom,
                query,
                start_s=start_s,
                end_s=end_s,
                step_s=args.step_s,
                timeout_s=args.timeout_s,
            )
        except urllib.error.URLError as exc:
            raise SystemExit(
                "Failed to query Prometheus at "
                f"{args.prom}. Ensure Prometheus is reachable (for example via kubectl port-forward). "
                f"Original error: {exc}"
            ) from exc
        (out_dir / f"{name}.json").write_text(
            json.dumps(payload, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        print(f"wrote {out_dir / f'{name}.json'}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
