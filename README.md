# Microservice Telemetry Dataset Kit

This repository provides a **reproducible end-to-end pipeline** to deploy a microservice benchmark (currently **Online Boutique**) and an observability stack (Istio + Prometheus + Jaeger) on local kind, replay realistic workload shapes derived from public web logs (ClarkNet/NASA), export **Prometheus metrics** and **Jaeger traces**, and organize them into **minute-level structured tables** ready for analysis and modeling.

---

## What you can do with it

- **Dataset generation / research reproducibility**: repeat the same workload + telemetry collection with consistent `run_id` / `scenario_id` and time-window metadata
- **AIOps / workload forecasting / capacity planning**: build supervised learning samples from service-level metrics and call-edge tables
- **Performance regression / comparative experiments**: replay fixed workload shapes and export comparable metrics + tracing evidence
- **Dependency and call dynamics analysis**: build service call edges from traces and align them with service-level time series

---

## Recommended workflow

Follow these docs in order to run the full workflow (deploy → inject → export → tables → plots):

- **Deploy & validate observability (Online Boutique)**: `benchmarks/online_boutique/docs/README.md`
- **Build workload schedules (ClarkNet/NASA → Locust schedule)**: `pipelines/traffic/README.md`
- **Inject & export raw telemetry (exported)**: `pipelines/export/README.md`
- **Canonicalize + tables + inspect (reports/plots)**: `pipelines/prepare/README.md`

---

## Online Boutique call graph (11 microservices)

Online Boutique is composed of 11 microservices written in different languages that communicate over gRPC/HTTP. The figure below shows the core call relationships (this repo builds corresponding call-edge tables from traces and aligns them with service-level metrics over time).

![Online Boutique call graph](imgs/image.png)

---

## What the exported data looks like

Example dataset root: `dataset/processed/online_boutique_clarknet`

- **raw (exported)**: `dataset/processed/<dataset>/exported/run_id=<run_id>/...`
  - Prometheus: `prom/<day>/<start>_<end>/*.json + meta.json`
  - Jaeger: `jaeger/<day>/<service>/<1min-slice>/{traces.json,meta.json}`
  - Injection summary: `run_manifest.json` / `injection_events.jsonl`
- **deduplicated (canonical)**: `dataset/processed/<dataset>/canonical/run_id=<run_id>/...`
- **tables**: `dataset/processed/<dataset>/tables/run_id=<run_id>/*.csv`
  - `service_minute_features.csv`: per-service minute feature table (demand, performance, reliability, state, dependency aggregates)
  - `service_call_edge_minute.csv`: per-minute caller→callee edge table (call_count / latency statistics / errors)

### Default collected metrics

Default exports (Prometheus, service/deployment level):

- **Traffic**: service in/out RPS
- **Latency**: mean / p95 / p99 latency
- **Reliability**: 5xx RPS, error rate, timeout rate
- **State**: CPU, memory, CPU throttling ratio, replicas spec/available, active node count

Default exports (Jaeger traces):

- **Service filter**: by default exports `frontend.ob` (used to build call edges and edge-level intensity/latency statistics)

---

## Dataset download

The corresponding open dataset is published on Zenodo (minute-level tables derived from Prometheus metrics + Jaeger traces under ClarkNet/NASA replay).

- Zenodo (DOI): [https://doi.org/10.5281/zenodo.20685204](https://doi.org/10.5281/zenodo.20685204)

Release notes:

- **Included**: weekday-only (no weekends), **10-day logical traffic** replay, **minute-level CSV tables**
- **Not included**: raw JSON slices (`exported/`) or deduplicated JSON (`canonical/`) — these can be regenerated using this repository pipeline

---

## Project structure

```text
.
├── README.md
├── benchmarks/
│   └── online_boutique/
│       ├── docs/README.md                 # deploy + observability validation
│       ├── manifests/                     # Prom/Jaeger/OTel/Istio manifests
│       ├── loadgen-locust/                # Locust injection scripts
│       ├── scripts/                       # helper scripts (e.g., kind config generation)
│       └── kind-config.template.yaml
├── pipelines/
│   ├── README.md                          # traffic → export → prepare overview
│   ├── traffic/README.md                  # public logs → schedule
│   ├── export/README.md                   # inject + export exported
│   └── prepare/README.md                  # canonicalize + tables + inspect
├── dataset/
│   ├── raw/                               # raw public datasets (NASA/ClarkNet)
│   └── processed/                         # outputs (exported/canonical/tables)
└── imgs/
```

---

## Dependencies

Deployment & collection:

- `docker`, `kind`, `kubectl`, `helm`, `istioctl`

Data processing:

- `python` (recommended: virtualenv/conda)

> Note: the deployment flow involves pulling and loading many container images. Depending on your network environment, a working image mirror/proxy may be required.

---

## Acknowledgements

- Online Boutique upstream microservices repository: [GoogleCloudPlatform/microservices-demo](https://github.com/GoogleCloudPlatform/microservices-demo)
