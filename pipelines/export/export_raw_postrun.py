#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from export_prom_metrics import build_queries
from service_urls import resolve_jaeger_url, resolve_prom_url
from time_range import parse_time_arg, to_rfc3339_utc


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def window_name(start_s: int, end_s: int) -> str:
    return f"{to_rfc3339_utc(start_s).replace(':', '-')}_{to_rfc3339_utc(end_s).replace(':', '-')}"


def iter_slices(start_s: int, end_s: int, slice_s: int):
    current = start_s
    while current < end_s:
        next_s = min(current + slice_s, end_s)
        yield current, next_s
        current = next_s


def append_metadata_args(command: list[str], run_id: str, scenario_id: str) -> list[str]:
    if run_id:
        command.extend(["--run-id", run_id])
    if scenario_id:
        command.extend(["--scenario-id", scenario_id])
    return command


def run_command(command: list[str], *, dry_run: bool) -> None:
    print("$ " + " ".join(command))
    if dry_run:
        return
    subprocess.run(command, check=True)


def completed_prom_export(out_dir: Path, *, namespace: str, window: str) -> bool:
    expected_files = ["meta.json", *[f"{name}.json" for name in build_queries(namespace, window)]]
    return all((out_dir / name).exists() for name in expected_files)


def completed_jaeger_export(out_dir: Path) -> bool:
    return (out_dir / "meta.json").exists() and (out_dir / "traces.json").exists()


def export_prom(
    *,
    script_path: Path,
    out_base_dir: Path,
    config: dict,
    run_id: str,
    scenario_id: str,
    start_s: int,
    end_s: int,
    chunk_minutes: int,
    dry_run: bool,
) -> None:
    chunk_s = chunk_minutes * 60
    if chunk_s <= 0:
        raise ValueError("--prom-chunk-minutes must be > 0.")

    prom_url = resolve_prom_url(config.get("url"))
    print(f"prom: exporting {to_rfc3339_utc(start_s)} -> {to_rfc3339_utc(end_s)} via {prom_url}")
    for chunk_start_s, chunk_end_s in iter_slices(start_s, end_s, chunk_s):
        date_dir = to_rfc3339_utc(chunk_start_s)[:10]
        out_dir = out_base_dir / "prom" / date_dir / window_name(chunk_start_s, chunk_end_s)
        if completed_prom_export(
            out_dir,
            namespace=str(config["namespace"]),
            window=str(config["rate_window"]),
        ):
            print(f"prom: skip existing {out_dir}")
            continue
        out_dir.mkdir(parents=True, exist_ok=True)

        command = [
            sys.executable,
            str(script_path),
            "--prom",
            prom_url,
            "--namespace",
            str(config["namespace"]),
            "--window",
            str(config["rate_window"]),
            "--start",
            to_rfc3339_utc(chunk_start_s),
            "--end",
            to_rfc3339_utc(chunk_end_s),
            "--step-s",
            str(config["step_s"]),
            "--timeout-s",
            str(config["timeout_s"]),
            "--out-dir",
            str(out_dir),
        ]
        append_metadata_args(command, run_id, scenario_id)
        run_command(command, dry_run=dry_run)


def export_jaeger(
    *,
    script_path: Path,
    out_base_dir: Path,
    config: dict,
    run_id: str,
    scenario_id: str,
    start_s: int,
    end_s: int,
    chunk_minutes: int,
    dry_run: bool,
) -> None:
    chunk_s = chunk_minutes * 60
    slice_s = int(config["slice_minutes"]) * 60
    if chunk_s <= 0:
        raise ValueError("--jaeger-chunk-minutes must be > 0.")
    if slice_s <= 0:
        raise ValueError("jaeger.slice_minutes must be > 0.")

    service = str(config["service"])
    jaeger_url = resolve_jaeger_url(config.get("url"))
    print(f"jaeger: exporting {to_rfc3339_utc(start_s)} -> {to_rfc3339_utc(end_s)} via {jaeger_url}")
    for chunk_start_s, chunk_end_s in iter_slices(start_s, end_s, chunk_s):
        print(f"jaeger: chunk {to_rfc3339_utc(chunk_start_s)} -> {to_rfc3339_utc(chunk_end_s)}")
        for slice_start_s, slice_end_s in iter_slices(chunk_start_s, chunk_end_s, slice_s):
            date_dir = to_rfc3339_utc(slice_start_s)[:10]
            out_dir = out_base_dir / "jaeger" / date_dir / service / window_name(slice_start_s, slice_end_s)
            if completed_jaeger_export(out_dir):
                print(f"jaeger: skip existing {out_dir}")
                continue
            out_dir.mkdir(parents=True, exist_ok=True)

            command = [
                sys.executable,
                str(script_path),
                "--jaeger",
                jaeger_url,
                "--service",
                service,
                "--start",
                to_rfc3339_utc(slice_start_s),
                "--end",
                to_rfc3339_utc(slice_end_s),
                "--limit",
                str(config["limit"]),
                "--timeout-s",
                str(config["timeout_s"]),
                "--out-dir",
                str(out_dir),
            ]
            append_metadata_args(command, run_id, scenario_id)
            run_command(command, dry_run=dry_run)


def main() -> int:
    script_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        description="Export Prometheus metrics and Jaeger traces after a load run completes."
    )
    parser.add_argument(
        "--run-dir",
        required=True,
        help="Run directory containing run_manifest.json and injection artifacts.",
    )
    parser.add_argument(
        "--config",
        default=str(script_dir / "export_raw_config.json"),
        help="Path to the export config JSON.",
    )
    parser.add_argument(
        "--only",
        choices=["all", "prom", "jaeger"],
        default="all",
        help="Only export one source. Default: all.",
    )
    parser.add_argument(
        "--out-base-dir",
        default="",
        help="Override output base directory. Default: <run-dir>.",
    )
    parser.add_argument(
        "--pad-before-minutes",
        type=int,
        default=5,
        help="Extra minutes to export before inject_start_utc. Default: 5.",
    )
    parser.add_argument(
        "--pad-after-minutes",
        type=int,
        default=5,
        help="Extra minutes to export after inject_end_utc. Default: 5.",
    )
    parser.add_argument(
        "--prom-chunk-minutes",
        type=int,
        default=60,
        help="Prometheus export chunk size in minutes. Default: 60.",
    )
    parser.add_argument(
        "--jaeger-chunk-minutes",
        type=int,
        default=10,
        help="Jaeger outer export chunk size in minutes. Default: 10.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned commands without executing them.",
    )
    args = parser.parse_args()

    if args.pad_before_minutes < 0 or args.pad_after_minutes < 0:
        raise SystemExit("--pad-before-minutes and --pad-after-minutes must be >= 0.")

    run_dir = Path(args.run_dir).expanduser().resolve()
    manifest_path = run_dir / "run_manifest.json"
    if not manifest_path.exists():
        raise SystemExit(f"run_manifest.json not found: {manifest_path}")

    config_path = Path(args.config).expanduser().resolve()
    if not config_path.exists():
        raise SystemExit(f"Config file not found: {config_path}")

    manifest = load_json(manifest_path)
    config = load_json(config_path)

    inject_start_utc = manifest.get("inject_start_utc")
    inject_end_utc = manifest.get("inject_end_utc")
    if not inject_start_utc or not inject_end_utc:
        raise SystemExit(
            f"run_manifest.json must contain inject_start_utc and inject_end_utc: {manifest_path}"
        )

    run_id = str(manifest.get("run_id") or config.get("metadata", {}).get("run_id") or "")
    scenario_id = str(
        manifest.get("scenario_id") or config.get("metadata", {}).get("scenario_id") or ""
    )

    start_s = parse_time_arg(inject_start_utc) - args.pad_before_minutes * 60
    end_s = parse_time_arg(inject_end_utc) + args.pad_after_minutes * 60
    if start_s >= end_s:
        raise SystemExit("Post-run export time range is invalid.")

    out_base_dir = Path(args.out_base_dir).expanduser().resolve() if args.out_base_dir else run_dir
    out_base_dir.mkdir(parents=True, exist_ok=True)

    prom_script = script_dir / "export_prom_metrics.py"
    jaeger_script = script_dir / "export_jaeger_traces.py"

    print(f"run_dir={run_dir}")
    print(f"out_base_dir={out_base_dir}")
    print(f"run_id={run_id}")
    print(f"scenario_id={scenario_id}")
    print(f"export_start_utc={to_rfc3339_utc(start_s)}")
    print(f"export_end_utc={to_rfc3339_utc(end_s)}")

    try:
        if args.only in ("all", "prom"):
            export_prom(
                script_path=prom_script,
                out_base_dir=out_base_dir,
                config=config["prom"],
                run_id=run_id,
                scenario_id=scenario_id,
                start_s=start_s,
                end_s=end_s,
                chunk_minutes=args.prom_chunk_minutes,
                dry_run=args.dry_run,
            )
        if args.only in ("all", "jaeger"):
            export_jaeger(
                script_path=jaeger_script,
                out_base_dir=out_base_dir,
                config=config["jaeger"],
                run_id=run_id,
                scenario_id=scenario_id,
                start_s=start_s,
                end_s=end_s,
                chunk_minutes=args.jaeger_chunk_minutes,
                dry_run=args.dry_run,
            )
    except KeyError as exc:
        raise SystemExit(f"Missing config key: {exc}") from exc
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"Post-run export failed: {exc}") from exc

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
