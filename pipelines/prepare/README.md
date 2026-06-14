# Prepare Pipelines

This stage turns the raw JSON exported in the `export` stage into:

1. `canonical`
2. `tables`

Responsibilities:

- Merge overlapping export windows
- Deduplicate raw Prometheus / Jaeger data
- Build minute-level service tables and service-to-service call edge tables

## Scripts in this stage

### 1. Canonicalize raw

- `canonicalize_prom_daily_json.py`
  - Input: `<dataset-root>/exported/run_id=<run_id>/prom/...`
  - Output: `dataset/processed/<dataset>/canonical/...`
  - Result: one JSON per (day, metric), deduplicated across overlapping windows

- `canonicalize_jaeger_daily_json.py`
  - Input: `<dataset-root>/exported/run_id=<run_id>/jaeger/...`
  - Output: `dataset/processed/<dataset>/canonical/...`
  - Result: one `traces.json` per (day, service), deduplicated by `(trace_id, span_id)`

### 2. Build intermediate tables

- `build_online_boutique_intermediate.py`
  - Input: `canonical/prom/...` and `canonical/jaeger/...`
  - Output:
    - `tables/run_id=<run_id>/service_minute_features.csv`
    - `tables/run_id=<run_id>/service_call_edge_minute.csv`
    - `tables/run_id=<run_id>/service_metadata.csv`
    - `tables/run_id=<run_id>/run_metadata.csv`
    - `tables/run_id=<run_id>/ready_summary.json`

## Common usage

### Process a specific `run_id`

Only the following dataset layout is supported:

- `dataset/processed/<dataset>/exported/run_id=<run_id>/...`

That means the scripts in this stage require:

- `--dataset-root`
- `--run-id`

Example:

```bash
DATASET_ROOT="dataset/processed/online_boutique_clarknet"
RUN_ID="clarknet-20260427_111536"
```

### Generate canonical raw

```bash
python pipelines/prepare/canonicalize_prom_daily_json.py \
  --dataset-root "$DATASET_ROOT" \
  --run-id "$RUN_ID"

python pipelines/prepare/canonicalize_jaeger_daily_json.py \
  --dataset-root "$DATASET_ROOT" \
  --run-id "$RUN_ID"
```

### Build intermediate tables (tables layer)

Default outputs (CSV):

- `tables/run_id=<run_id>/service_minute_features.csv`
- `tables/run_id=<run_id>/service_call_edge_minute.csv`
- `tables/run_id=<run_id>/service_metadata.csv`
- `tables/run_id=<run_id>/run_metadata.csv`

```bash
python pipelines/prepare/build_online_boutique_intermediate.py \
  --dataset-root "$DATASET_ROOT" \
  --run-id "$RUN_ID"
```

### Core feature inspection + time-series plots

Outputs:

- `report.md` / `report.json`
- Time-series plots (PNG) for each service and feature group

Defaults:

- Inspects and plots **one day** at a time
- If `--day` is not provided:
  - For the **single-file CSV** layout: picks the latest day present in the data
  - (Legacy-compatible) For the partitioned `day=.../part-000.csv` layout: picks the latest `day=...` directory

```bash
python pipelines/prepare/inspect_and_plot_core_features.py \
  --dataset-root "$DATASET_ROOT" \
  --run-id "$RUN_ID" \
  --day 2026-04-27
```

Common arguments:

- `--day`: day string like `2026-04-22`. If empty, the script selects the latest day by default.
- `--services`: only plot specified services (comma-separated), e.g.:

```bash
python pipelines/prepare/inspect_and_plot_core_features.py \
  --dataset-root "$DATASET_ROOT" \
  --run-id "$RUN_ID" \
  --day 2026-04-27 \
  --services frontend,checkoutservice
```

- `--max-points N`: plot only the last N points per service (useful for long runs)
- `--out-dir`: custom output directory
- `--dataset-root`: dataset root (e.g. `dataset/processed/online_boutique_clarknet`), used with `--run-id`
- `--run-id`: run identifier
