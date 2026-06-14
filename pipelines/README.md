# Pipelines (Overview)

This directory contains the complete pipeline that turns an Online Boutique experiment from a “real-world workload curve” into “minute-level tables”.

Conventions:

- All command blocks in this directory are intended to be copied and executed from the **repository root** by default.
- Outputs are written under `dataset/processed/<dataset>/...`.

---

## End-to-end flow (recommended order)

### 1) traffic: public web logs → Locust schedule

Entry doc: `pipelines/traffic/README.md`

- **Input**: `dataset/raw/{NASA-HTTP,ClarkNet-HTTP}/...`
- **Output**: `dataset/processed/traffic/*/schedules/*.csv`

---

### 2) export: inject workload + export raw telemetry (exported)

Entry doc: `pipelines/export/README.md`

- **Prerequisites**:
  - You have completed deployment (see `benchmarks/online_boutique/docs/README.md`).
  - You have generated schedules (see `pipelines/traffic/README.md`).
- **Output**: `dataset/processed/<dataset>/exported/run_id=<run_id>/...`
  - `prom/<day>/<start>_<end>/*_metrics.json + meta.json`
  - `jaeger/<day>/<service>/<1min-slice>/traces.json + meta.json`
  - `run_manifest.json` / `injection_events.jsonl`

---

### 3) prepare: canonicalize + build tables + inspect

Entry doc: `pipelines/prepare/README.md`

- **Input**: `dataset/processed/<dataset>/exported/run_id=<run_id>/...`
- **Output**:
  - `dataset/processed/<dataset>/canonical/run_id=<run_id>/...`
  - `dataset/processed/<dataset>/tables/run_id=<run_id>/*.csv`
  - `dataset/processed/<dataset>/tables/run_id=<run_id>/inspect_plots/day=<day>/*`

---

## 5-minute smoke (shortest end-to-end path)

1. Deploy: `benchmarks/online_boutique/docs/README.md`
2. Build a ClarkNet schedule: `pipelines/traffic/README.md`
3. Run ClarkNet smoke 5m + post-run export: `pipelines/export/README.md` (ClarkNet → Smoke)
4. Canonicalize + tables + inspect: `pipelines/prepare/README.md`
