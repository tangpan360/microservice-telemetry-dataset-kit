#!/usr/bin/env python3

import argparse
import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from service_urls import default_jaeger_url
from time_range import resolve_time_range, to_rfc3339_utc

DEFAULT_TRACE_LIMIT = 10_000


def http_get_json(url: str, timeout_s: int = 60) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_traces(
    jaeger_base: str,
    *,
    service: str,
    start_us: int,
    end_us: int,
    limit: int,
    timeout_s: int = 60,
) -> dict:
    params = {
        "service": service,
        "start": start_us,
        "end": end_us,
        "limit": limit,
    }
    encoded = urllib.parse.urlencode(params)
    url = f"{jaeger_base.rstrip('/')}/api/traces?{encoded}"
    return http_get_json(url, timeout_s=timeout_s)


def default_out_dir() -> Path:
    repo_root = Path(__file__).resolve().parents[2]
    ts = time.strftime("%Y%m%d_%H%M%S")
    return repo_root / "runs" / "export_jaeger_traces" / ts


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export Jaeger traces as raw JSON."
    )
    parser.add_argument(
        "--jaeger",
        default=default_jaeger_url(),
        help="Jaeger query base URL. Default: http://127.0.0.1:16686.",
    )
    parser.add_argument(
        "--service",
        default="frontend.ob",
        help="Jaeger service name filter. Default: frontend.ob.",
    )
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
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_TRACE_LIMIT,
        help=f"Max traces to return. Default: {DEFAULT_TRACE_LIMIT}.",
    )
    parser.add_argument("--run-id", default="", help="Optional experiment run identifier.")
    parser.add_argument("--scenario-id", default="", help="Optional experiment scenario identifier.")
    parser.add_argument(
        "--timeout-s",
        type=int,
        default=180,
        help="HTTP timeout in seconds for the Jaeger query. Default: 180.",
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
    if args.limit <= 0:
        raise SystemExit("--limit must be > 0.")
    if args.timeout_s <= 0:
        raise SystemExit("--timeout-s must be > 0.")
    start_us = start_s * 1_000_000
    end_us = end_s * 1_000_000

    try:
        payload = fetch_traces(
            args.jaeger,
            service=args.service,
            start_us=start_us,
            end_us=end_us,
            limit=args.limit,
            timeout_s=args.timeout_s,
        )
    except (urllib.error.URLError, http.client.RemoteDisconnected) as exc:
        raise SystemExit(
            "Failed to query Jaeger at "
            f"{args.jaeger}. Ensure Jaeger Query is reachable (for example via kubectl port-forward). "
            "If this happens with a very large --limit, try a smaller time window or a lower limit. "
            f"Original error: {exc}"
        ) from exc

    returned_traces = len(payload.get("data") or [])

    meta = {
        "jaeger": args.jaeger,
        "service": args.service,
        "run_id": args.run_id,
        "scenario_id": args.scenario_id,
        "minutes": args.minutes,
        "hours": args.hours,
        "days": args.days,
        "window_unit": window_unit,
        "window_value": window_value,
        "time_mode": time_mode,
        "start": args.start,
        "end": args.end,
        "limit": args.limit,
        "timeout_s": args.timeout_s,
        "start_s": start_s,
        "end_s": end_s,
        "start_us": start_us,
        "end_us": end_us,
        "start_utc": to_rfc3339_utc(start_s),
        "end_utc": to_rfc3339_utc(end_s),
        "returned_traces": returned_traces,
        "hit_limit": returned_traces >= args.limit,
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    (out_dir / "traces.json").write_text(
        json.dumps(payload, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {out_dir / 'traces.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
