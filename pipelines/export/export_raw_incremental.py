#!/usr/bin/env python3

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from service_urls import resolve_jaeger_url, resolve_prom_url
from time_range import to_rfc3339_utc


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def floor_epoch(epoch_s: int, step_s: int) -> int:
    if step_s <= 0:
        raise ValueError("step_s must be > 0.")
    return epoch_s - (epoch_s % step_s)


def window_name(start_s: int, end_s: int) -> str:
    return f"{to_rfc3339_utc(start_s).replace(':', '-')}_{to_rfc3339_utc(end_s).replace(':', '-')}"


def load_state(path: Path) -> int | None:
    if not path.exists():
        return None
    data = load_json(path)
    value = data.get("last_end_s")
    return int(value) if value is not None else None


def save_state(path: Path, end_s: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_end_s": end_s,
        "last_end_utc": to_rfc3339_utc(end_s),
        "updated_at_utc": to_rfc3339_utc(int(time.time())),
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def ensure_positive_int(name: str, value: int) -> None:
    if int(value) <= 0:
        raise ValueError(f"{name} must be > 0.")


def compute_window(
    *,
    now_s: int,
    interval_minutes: int,
    bootstrap_minutes: int,
    lag_minutes: int,
    overlap_minutes: int,
    align_s: int,
    last_end_s: int | None,
) -> tuple[int, int] | None:
    ensure_positive_int("interval_minutes", interval_minutes)
    ensure_positive_int("bootstrap_minutes", bootstrap_minutes)
    ensure_positive_int("lag_minutes", lag_minutes)
    if overlap_minutes < 0:
        raise ValueError("overlap_minutes must be >= 0.")

    safe_end_s = floor_epoch(now_s - lag_minutes * 60, align_s)
    if safe_end_s <= 0:
        return None

    if last_end_s is None:
        start_s = safe_end_s - bootstrap_minutes * 60
    else:
        start_s = last_end_s - overlap_minutes * 60

    start_s = max(0, floor_epoch(start_s, align_s))
    if start_s >= safe_end_s:
        return None
    return start_s, safe_end_s


def run_command(command: list[str], *, dry_run: bool) -> None:
    print("$ " + " ".join(command))
    if dry_run:
        return
    subprocess.run(command, check=True)


def append_metadata_args(command: list[str], metadata: dict) -> list[str]:
    mapping = {
        "run_id": "--run-id",
        "scenario_id": "--scenario-id",
    }
    for key, flag in mapping.items():
        value = metadata.get(key, "")
        if value == "" or value is None:
            continue
        command.extend([flag, str(value)])
    return command


def run_prom(
    *,
    repo_root: Path,
    script_path: Path,
    config: dict,
    out_base_dir: Path,
    state_dir: Path,
    dry_run: bool,
    now_s: int,
) -> None:
    if not config.get("enabled", True):
        print("prom: disabled")
        return

    state_path = state_dir / "prom_last_end.json"
    last_end_s = load_state(state_path)
    step_s = int(config["step_s"])
    ensure_positive_int("prom.step_s", step_s)
    window = compute_window(
        now_s=now_s,
        interval_minutes=int(config["interval_minutes"]),
        bootstrap_minutes=int(config.get("bootstrap_minutes", config["interval_minutes"])),
        lag_minutes=int(config["lag_minutes"]),
        overlap_minutes=int(config["overlap_minutes"]),
        align_s=step_s,
        last_end_s=last_end_s,
    )
    if window is None:
        print("prom: no new stable window to export")
        return

    start_s, end_s = window
    date_dir = to_rfc3339_utc(start_s)[:10]
    out_dir = out_base_dir / "prom" / date_dir / window_name(start_s, end_s)
    out_dir.mkdir(parents=True, exist_ok=True)

    prom_url = resolve_prom_url(config.get("url"))
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
        to_rfc3339_utc(start_s),
        "--end",
        to_rfc3339_utc(end_s),
        "--step-s",
        str(step_s),
        "--timeout-s",
        str(config["timeout_s"]),
        "--out-dir",
        str(out_dir),
    ]
    append_metadata_args(command, config.get("metadata", {}))
    print(f"prom: exporting {to_rfc3339_utc(start_s)} -> {to_rfc3339_utc(end_s)} via {prom_url}")
    run_command(command, dry_run=dry_run)
    if not dry_run:
        save_state(state_path, end_s)


def iter_slices(start_s: int, end_s: int, slice_s: int):
    current = start_s
    while current < end_s:
        next_s = min(current + slice_s, end_s)
        yield current, next_s
        current = next_s


def run_jaeger(
    *,
    repo_root: Path,
    script_path: Path,
    config: dict,
    out_base_dir: Path,
    state_dir: Path,
    dry_run: bool,
    now_s: int,
) -> None:
    if not config.get("enabled", True):
        print("jaeger: disabled")
        return

    state_path = state_dir / "jaeger_last_end.json"
    last_end_s = load_state(state_path)
    slice_minutes = int(config["slice_minutes"])
    ensure_positive_int("jaeger.slice_minutes", slice_minutes)
    slice_s = slice_minutes * 60

    window = compute_window(
        now_s=now_s,
        interval_minutes=int(config["interval_minutes"]),
        bootstrap_minutes=int(config.get("bootstrap_minutes", config["interval_minutes"])),
        lag_minutes=int(config["lag_minutes"]),
        overlap_minutes=int(config["overlap_minutes"]),
        align_s=slice_s,
        last_end_s=last_end_s,
    )
    if window is None:
        print("jaeger: no new stable window to export")
        return

    start_s, end_s = window
    jaeger_url = resolve_jaeger_url(config.get("url"))
    print(f"jaeger: exporting {to_rfc3339_utc(start_s)} -> {to_rfc3339_utc(end_s)} via {jaeger_url}")

    service = str(config["service"])
    for slice_start_s, slice_end_s in iter_slices(start_s, end_s, slice_s):
        date_dir = to_rfc3339_utc(slice_start_s)[:10]
        out_dir = out_base_dir / "jaeger" / date_dir / service / window_name(slice_start_s, slice_end_s)
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
        append_metadata_args(command, config.get("metadata", {}))
        run_command(command, dry_run=dry_run)

    if not dry_run:
        save_state(state_path, end_s)


def main() -> int:
    repo_root = Path(__file__).resolve().parents[2]
    script_dir = Path(__file__).resolve().parent

    parser = argparse.ArgumentParser(
        description="Incrementally export raw Prometheus metrics and Jaeger traces."
    )
    parser.add_argument("--run-id", default="", help="Optional experiment run identifier override.")
    parser.add_argument(
        "--scenario-id",
        default="",
        help="Optional experiment scenario identifier override.",
    )
    parser.add_argument(
        "--config",
        default=str(script_dir / "export_raw_config.json"),
        help="Path to the incremental export config JSON.",
    )
    parser.add_argument(
        "--only",
        choices=["all", "prom", "jaeger"],
        default="all",
        help="Only run one source. Default: all.",
    )
    parser.add_argument(
        "--out-base-dir",
        default=str(repo_root / "runs" / "export_raw"),
        help="Base output directory for raw JSON exports.",
    )
    parser.add_argument(
        "--state-dir",
        default="",
        help="State directory. Default: <out-base-dir>/state",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned export commands without executing them.",
    )
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        raise SystemExit(f"Config file not found: {config_path}")
    config = load_json(config_path)

    # Optional CLI overrides, so docs don't require editing config.
    if args.run_id or args.scenario_id:
        meta = dict(config.get("metadata", {}) or {})
        if args.run_id:
            meta["run_id"] = args.run_id
        if args.scenario_id:
            meta["scenario_id"] = args.scenario_id
        config["metadata"] = meta
        for key in ("prom", "jaeger"):
            if key in config and isinstance(config.get(key), dict):
                config[key]["metadata"] = meta

    out_base_dir = Path(args.out_base_dir)
    state_dir = Path(args.state_dir) if args.state_dir else out_base_dir / "state"
    out_base_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)

    now_s = int(time.time())
    prom_script = script_dir / "export_prom_metrics.py"
    jaeger_script = script_dir / "export_jaeger_traces.py"

    try:
        if args.only in ("all", "prom"):
            run_prom(
                repo_root=repo_root,
                script_path=prom_script,
                config=config["prom"],
                out_base_dir=out_base_dir,
                state_dir=state_dir,
                dry_run=args.dry_run,
                now_s=now_s,
            )
        if args.only in ("all", "jaeger"):
            run_jaeger(
                repo_root=repo_root,
                script_path=jaeger_script,
                config=config["jaeger"],
                out_base_dir=out_base_dir,
                state_dir=state_dir,
                dry_run=args.dry_run,
                now_s=now_s,
            )
    except KeyError as exc:
        raise SystemExit(f"Missing config key: {exc}") from exc
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"Incremental export failed: {exc}") from exc

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
