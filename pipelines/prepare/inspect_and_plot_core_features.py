#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path

import pandas as pd


DEFAULT_CORE_FEATURE_GROUPS: dict[str, list[str]] = {
    "demand": ["service_in_rps", "service_out_rps"],
    "performance": [
        "service_latency_mean_ms",
        "service_latency_p95_ms",
        "service_latency_p99_ms",
    ],
    "reliability": ["service_error_rate", "service_timeout_rate"],
    "state": [
        "cpu_cores",
        "memory_working_set_bytes",
        "replica_count",
        "ready_replica_count",
        "cpu_throttling_ratio",
        "active_node_count",
    ],
    "dependency": [
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
    ],
}

NONNEGATIVE_PREFIXES = (
    "service_",
    "cpu_",
    "memory_",
    "replica",
    "ready_",
    "active_node",
    "upstream_",
    "downstream_",
    "top",
)


def latest_day_dir(base: Path) -> Path:
    day_dirs = sorted([p for p in base.iterdir() if p.is_dir() and p.name.startswith("day=")])
    if not day_dirs:
        raise SystemExit(f"No day directories found under: {base}")
    return day_dirs[-1]


def read_table(path: Path) -> pd.DataFrame:
    return pd.read_csv(path)


def resolve_tables_layout(tables_root: Path) -> str:
    """
    Support both historical partitioned layout and current single-CSV layout.

    - partitioned:
        <tables_root>/service_minute_features/day=YYYY-MM-DD/part-000.csv
        <tables_root>/service_call_edge_minute/day=YYYY-MM-DD/part-000.csv
    - single_csv:
        <tables_root>/service_minute_features.csv
        <tables_root>/service_call_edge_minute.csv
    """
    svc_dir = tables_root / "service_minute_features"
    svc_csv = tables_root / "service_minute_features.csv"
    if svc_dir.exists() and svc_dir.is_dir():
        return "partitioned"
    if svc_csv.exists() and svc_csv.is_file():
        return "single_csv"
    raise SystemExit(
        "Cannot find service_minute_features table under tables root. "
        f"Expected either {svc_dir}/day=.../part-000.csv or {svc_csv}"
    )


def pick_latest_day_from_df(service_df: pd.DataFrame) -> str:
    if service_df.empty:
        raise SystemExit("Empty service_minute_features table; cannot infer latest day.")
    if "timestamp_bucket_utc" not in service_df.columns:
        raise SystemExit("Missing timestamp_bucket_utc in service_minute_features table.")
    ts = service_df["timestamp_bucket_utc"]
    if not pd.api.types.is_datetime64_any_dtype(ts):
        ts = pd.to_datetime(ts, utc=True, errors="coerce")
    days = ts.dt.strftime("%Y-%m-%d").dropna()
    if days.empty:
        raise SystemExit("No parseable timestamp_bucket_utc values; cannot infer latest day.")
    return str(days.max())


def filter_df_by_day(df: pd.DataFrame, day: str) -> pd.DataFrame:
    if df.empty:
        return df
    if "timestamp_bucket_utc" not in df.columns:
        return df
    ts = df["timestamp_bucket_utc"]
    if not pd.api.types.is_datetime64_any_dtype(ts):
        ts = pd.to_datetime(ts, utc=True, errors="coerce")
    return df[ts.dt.strftime("%Y-%m-%d") == day].copy()


def ensure_timestamp_utc(df: pd.DataFrame) -> pd.DataFrame:
    if "timestamp_bucket_utc" in df.columns and df["timestamp_bucket_utc"].notna().any():
        df["timestamp_bucket_utc"] = pd.to_datetime(df["timestamp_bucket_utc"], utc=True, errors="coerce")
        return df
    if "timestamp_bucket_s" in df.columns:
        df["timestamp_bucket_utc"] = pd.to_datetime(df["timestamp_bucket_s"], unit="s", utc=True, errors="coerce")
    return df


def gap_stats_per_service(service_df: pd.DataFrame, bucket_seconds: int = 60) -> dict:
    out: dict[str, dict] = {}
    if service_df.empty:
        return out

    for service, g in service_df.groupby("service"):
        ts = sorted(pd.unique(g["timestamp_bucket_s"].astype(int)))
        if len(ts) <= 1:
            out[service] = {"points": len(ts), "gap_minutes": 0, "max_gap_minutes": 0}
            continue
        diffs = [b - a for a, b in zip(ts, ts[1:])]
        gaps = [d for d in diffs if d > bucket_seconds]
        out[service] = {
            "points": len(ts),
            "gap_minutes": int(sum((d // bucket_seconds) - 1 for d in gaps)),
            "max_gap_minutes": int((max(gaps) // bucket_seconds) - 1) if gaps else 0,
        }
    return out


def duplicate_key_count(df: pd.DataFrame, key_cols: list[str]) -> int:
    if df.empty:
        return 0
    return int(df.duplicated(subset=key_cols).sum())


def column_quality_summary(df: pd.DataFrame, numeric_cols: list[str]) -> dict[str, dict]:
    summary: dict[str, dict] = {}
    n = len(df)
    if n == 0:
        return summary

    for col in numeric_cols:
        if col not in df.columns:
            continue
        s = pd.to_numeric(df[col], errors="coerce")
        na = int(s.isna().sum())
        zeros = int((s == 0).sum(skipna=True))
        finite = s.dropna()
        summary[col] = {
            "missing": na,
            "missing_ratio": float(na / n),
            "zero_ratio": float(zeros / n),
            "min": float(finite.min()) if not finite.empty else math.nan,
            "p50": float(finite.median()) if not finite.empty else math.nan,
            "p95": float(finite.quantile(0.95)) if not finite.empty else math.nan,
            "max": float(finite.max()) if not finite.empty else math.nan,
        }
    return summary


def nonnegative_violations(df: pd.DataFrame, numeric_cols: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    if df.empty:
        return out
    for col in numeric_cols:
        if col not in df.columns:
            continue
        if not col.startswith(NONNEGATIVE_PREFIXES):
            continue
        s = pd.to_numeric(df[col], errors="coerce")
        out[col] = int((s < 0).sum(skipna=True))
    return {k: v for k, v in out.items() if v}


@dataclass
class Report:
    day: str
    input_format: str
    service_rows: int
    service_cols: int
    service_count: int
    service_time_range_utc: list[str]
    edge_rows: int
    edge_cols: int
    edge_time_range_utc: list[str]
    run_id_values: list[str]
    scenario_id_values: list[str]
    duplicates: dict
    gaps: dict
    column_quality: dict
    nonnegative_violations: dict


def plot_service_timeseries(
    *,
    df: pd.DataFrame,
    service: str,
    groups: dict[str, list[str]],
    out_dir: Path,
    max_points: int | None = None,
) -> list[Path]:
    import matplotlib.pyplot as plt

    g = df[df["service"] == service].sort_values("timestamp_bucket_utc").copy()
    if max_points and len(g) > max_points:
        g = g.iloc[-max_points:]

    paths: list[Path] = []
    for group_name, cols in groups.items():
        cols_present = [c for c in cols if c in g.columns]
        if not cols_present:
            continue

        fig, axes = plt.subplots(len(cols_present), 1, figsize=(12, 2.2 * len(cols_present)), sharex=True)
        if len(cols_present) == 1:
            axes = [axes]
        for ax, col in zip(axes, cols_present):
            ax.plot(g["timestamp_bucket_utc"], pd.to_numeric(g[col], errors="coerce"), linewidth=1.2)
            ax.set_ylabel(col)
            ax.grid(True, alpha=0.25)
        axes[-1].set_xlabel("time (UTC)")
        fig.suptitle(f"{service} - {group_name}", y=0.995)
        fig.tight_layout()

        out_path = out_dir / "service_plots" / service / f"{group_name}.png"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(out_path, dpi=160)
        plt.close(fig)
        paths.append(out_path)
    return paths


def plot_global_overview(
    *,
    df: pd.DataFrame,
    cols: list[str],
    out_dir: Path,
) -> Path | None:
    import matplotlib.pyplot as plt

    cols_present = [c for c in cols if c in df.columns]
    if not cols_present:
        return None

    g = df.groupby("timestamp_bucket_utc", as_index=False)[cols_present].mean(numeric_only=True)
    fig, axes = plt.subplots(len(cols_present), 1, figsize=(12, 2.2 * len(cols_present)), sharex=True)
    if len(cols_present) == 1:
        axes = [axes]
    for ax, col in zip(axes, cols_present):
        ax.plot(g["timestamp_bucket_utc"], pd.to_numeric(g[col], errors="coerce"), linewidth=1.2)
        ax.set_ylabel(f"mean({col})")
        ax.grid(True, alpha=0.25)
    axes[-1].set_xlabel("time (UTC)")
    fig.suptitle("Global overview (mean across services)", y=0.995)
    fig.tight_layout()

    out_path = out_dir / "global_overview_mean.png"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=160)
    plt.close(fig)
    return out_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect Online Boutique tables core features and plot time series."
    )
    parser.add_argument(
        "--dataset-root",
        required=True,
        help="Dataset root, e.g. dataset/processed/online_boutique_clarknet.",
    )
    parser.add_argument(
        "--run-id",
        required=True,
        help="Run identifier under <dataset-root>/tables.",
    )
    parser.add_argument(
        "--day",
        default="",
        help='Day string like "2026-04-22". If empty: partitioned layout -> latest day directory; single-CSV layout -> latest day in data.',
    )
    parser.add_argument(
        "--out-dir",
        default="",
        help="Output directory for report and plots. Default: <tables-root>/inspect_plots/day=<day>/",
    )
    parser.add_argument(
        "--services",
        default="",
        help="Comma-separated service names to plot. Default: plot all services in file.",
    )
    parser.add_argument(
        "--max-points",
        type=int,
        default=0,
        help="If >0, only plot last N points per service (for very long runs).",
    )
    args = parser.parse_args()

    dataset_root = Path(args.dataset_root).expanduser().resolve()
    tables_root = dataset_root / "tables" / f"run_id={args.run_id.strip()}"

    layout = resolve_tables_layout(tables_root)
    day = args.day.strip()

    if layout == "partitioned":
        svc_base = tables_root / "service_minute_features"
        edge_base = tables_root / "service_call_edge_minute"

        day_dir = svc_base / f"day={day}" if day else latest_day_dir(svc_base)
        day = day_dir.name.split("day=", 1)[-1]

        svc_path = day_dir / "part-000.csv"
        edge_path = edge_base / f"day={day}" / "part-000.csv"
        service_df = read_table(svc_path)
        edge_df = read_table(edge_path) if edge_path.exists() else pd.DataFrame()
    else:
        svc_path = tables_root / "service_minute_features.csv"
        edge_path = tables_root / "service_call_edge_minute.csv"
        service_df = read_table(svc_path)
        edge_df = read_table(edge_path) if edge_path.exists() else pd.DataFrame()

    service_df = ensure_timestamp_utc(service_df)
    edge_df = ensure_timestamp_utc(edge_df) if not edge_df.empty else edge_df

    if layout == "single_csv":
        if not day:
            day = pick_latest_day_from_df(service_df)
        service_df = filter_df_by_day(service_df, day)
        edge_df = filter_df_by_day(edge_df, day) if not edge_df.empty else edge_df

    numeric_cols = [c for c in service_df.columns if c not in {"service", "timestamp_bucket_utc", "day", "run_id", "scenario_id"}]
    numeric_cols = [c for c in numeric_cols if pd.api.types.is_numeric_dtype(service_df[c]) or c.endswith(("_rps", "_ms", "_rate", "_count", "_ratio", "_sin", "_cos"))]

    run_id_values = sorted({str(x) for x in service_df.get("run_id", pd.Series([], dtype=str)).fillna("").unique()})
    scenario_id_values = sorted({str(x) for x in service_df.get("scenario_id", pd.Series([], dtype=str)).fillna("").unique()})

    report = Report(
        day=day,
        input_format=("csv_partitioned" if layout == "partitioned" else "csv_single_file"),
        service_rows=int(len(service_df)),
        service_cols=int(len(service_df.columns)),
        service_count=int(service_df["service"].nunique()) if not service_df.empty else 0,
        service_time_range_utc=[
            str(service_df["timestamp_bucket_utc"].min()) if not service_df.empty else "",
            str(service_df["timestamp_bucket_utc"].max()) if not service_df.empty else "",
        ],
        edge_rows=int(len(edge_df)),
        edge_cols=int(len(edge_df.columns)) if not edge_df.empty else 0,
        edge_time_range_utc=[
            str(edge_df["timestamp_bucket_utc"].min()) if not edge_df.empty else "",
            str(edge_df["timestamp_bucket_utc"].max()) if not edge_df.empty else "",
        ],
        run_id_values=run_id_values,
        scenario_id_values=scenario_id_values,
        duplicates={
            "service_minute_features": duplicate_key_count(service_df, ["timestamp_bucket_s", "service"]) if not service_df.empty else 0,
            "service_call_edge_minute": duplicate_key_count(edge_df, ["timestamp_bucket_s", "caller_service", "callee_service"]) if not edge_df.empty else 0,
        },
        gaps=gap_stats_per_service(service_df, bucket_seconds=60),
        column_quality=column_quality_summary(service_df, numeric_cols),
        nonnegative_violations=nonnegative_violations(service_df, numeric_cols),
    )

    out_dir = Path(args.out_dir) if args.out_dir else (tables_root / "inspect_plots" / f"day={day}")
    out_dir.mkdir(parents=True, exist_ok=True)

    (out_dir / "report.json").write_text(json.dumps(asdict(report), indent=2, ensure_ascii=False), encoding="utf-8")

    md_lines = []
    md_lines.append(f"# Intermediate core feature inspection ({day})")
    md_lines.append("")
    md_lines.append(f"- service rows: **{report.service_rows}**, services: **{report.service_count}**")
    md_lines.append(f"- service time range (UTC): **{report.service_time_range_utc[0]} ~ {report.service_time_range_utc[1]}**")
    md_lines.append(f"- edge rows: **{report.edge_rows}**")
    md_lines.append(f"- edge time range (UTC): **{report.edge_time_range_utc[0]} ~ {report.edge_time_range_utc[1]}**")
    md_lines.append(f"- duplicated keys: service={report.duplicates['service_minute_features']}, edge={report.duplicates['service_call_edge_minute']}")
    md_lines.append(f"- run_id values: `{report.run_id_values}`")
    md_lines.append(f"- scenario_id values: `{report.scenario_id_values}`")
    if report.nonnegative_violations:
        md_lines.append("")
        md_lines.append("## Non-negative violations")
        for k, v in sorted(report.nonnegative_violations.items(), key=lambda x: -x[1]):
            md_lines.append(f"- {k}: {v}")
    md_lines.append("")
    md_lines.append("## Gaps per service (minutes missing)")
    for svc, s in sorted(report.gaps.items(), key=lambda x: (-x[1].get("gap_minutes", 0), x[0])):
        md_lines.append(f"- {svc}: gap_minutes={s['gap_minutes']}, max_gap_minutes={s['max_gap_minutes']}, points={s['points']}")
    (out_dir / "report.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")

    if service_df.empty:
        print(f"[inspect] empty service_df, wrote report to: {out_dir}")
        return 0

    if args.services.strip():
        services = [s.strip() for s in args.services.split(",") if s.strip()]
    else:
        services = sorted(service_df["service"].unique())

    max_points = args.max_points if args.max_points and args.max_points > 0 else None
    for svc in services:
        plot_service_timeseries(
            df=service_df,
            service=svc,
            groups=DEFAULT_CORE_FEATURE_GROUPS,
            out_dir=out_dir,
            max_points=max_points,
        )

    plot_global_overview(
        df=service_df,
        cols=DEFAULT_CORE_FEATURE_GROUPS["demand"] + DEFAULT_CORE_FEATURE_GROUPS["performance"] + DEFAULT_CORE_FEATURE_GROUPS["state"],
        out_dir=out_dir,
    )

    print(f"[inspect] wrote report and plots to: {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
