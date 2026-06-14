#!/usr/bin/env python3

from __future__ import annotations

import argparse
from pathlib import Path


PLACEHOLDER = "__KIND_OB_VOLUMES_HOST_PATH__"
DEFAULT_OUT = "kind-ob-config.yaml"


def repo_root() -> Path:
    # benchmarks/online_boutique/scripts/generate_kind_config.py -> repo root is parents[3]
    return Path(__file__).resolve().parents[3]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate a portable kind config by filling absolute hostPath values."
    )
    parser.add_argument(
        "--template",
        type=Path,
        default=repo_root() / "benchmarks/online_boutique/kind-config.template.yaml",
        help="Path to the kind config template (contains placeholders).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=repo_root() / "benchmarks/online_boutique" / DEFAULT_OUT,
        help="Output path for the generated kind config. Default: benchmarks/online_boutique/kind-ob-config.yaml",
    )
    parser.add_argument(
        "--volumes-dir",
        type=Path,
        default=repo_root() / ".local/kind-ob-volumes",
        help="Host directory mounted into kind nodes for local-path PV. Default: .local/kind-ob-volumes",
    )
    args = parser.parse_args()

    template_path = args.template.expanduser().resolve()
    out_path = args.out.expanduser().resolve()
    volumes_dir = args.volumes_dir.expanduser().resolve()

    if not template_path.exists():
        raise SystemExit(f"Template not found: {template_path}")

    volumes_dir.mkdir(parents=True, exist_ok=True)

    text = template_path.read_text(encoding="utf-8")
    if PLACEHOLDER not in text:
        raise SystemExit(
            f"Template does not contain placeholder {PLACEHOLDER!r}: {template_path}"
        )

    rendered = text.replace(PLACEHOLDER, str(volumes_dir))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(rendered, encoding="utf-8")

    print(str(out_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

