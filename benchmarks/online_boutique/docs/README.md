# Step-by-step: Deploy Online Boutique + Istio + Prometheus (deployment & observability validation)

---

### 1) Deployment strategy used in this guide

`Online Boutique` is deployed and validated in an **isolated kind cluster**.

Conventions used throughout this guide:
- kind 集群名：`ob`
- kubectl context：`kind-ob`
- 应用 namespace：`ob`
- 监控 namespace：`monitoring`
- kind 配置模板：`benchmarks/online_boutique/kind-config.template.yaml`
- kind 配置（运行时生成）：`benchmarks/online_boutique/kind-ob-config.yaml`

---

### 2) Prerequisites

Make sure the following tools are available:

```bash
kubectl version --client
kind version
helm version
istioctl version --remote=false
docker version
```

### 3) Clean up an existing `ob` cluster/namespace (if any)

If you have previously deployed `Online Boutique`, it is recommended to clean up the old environment first to avoid conflicts.

Delete the old cluster (if it exists):

```bash
kind delete cluster --name ob || true
```

If you prefer not to delete the entire cluster and only want to clean up the old namespace, run the following under the corresponding context:

```bash
kubectl delete ns ob --wait=true || true
```

It is OK if this step reports `NotFound`.

---

### 4) Create an isolated kind cluster

This guide uses a 3-node kind cluster:
- 1 个 control-plane
- 2 个 worker

We use a kind config template in this repository and generate a “portable” kind config at runtime:

```bash
sed -n '1,120p' benchmarks/online_boutique/kind-config.template.yaml
```

`kind-config.template.yaml` is a kind cluster config template. It declares node roles/count, host directory mounts (`extraMounts`), host port mappings (`extraPortMappings`), and registry mirrors to make the environment reproducible.

Because this config uses `extraMounts` to mount a host directory into the kind node containers (kind “nodes” are Docker containers), make sure the host directory exists before creating the cluster. Here, `.local/kind-ob-volumes` is used to back local persistent volumes inside the kind cluster (e.g., PV data used by local-path-provisioner).

From the repository root:

```bash
mkdir -p .local/kind-ob-volumes
```

Note: kind requires `extraMounts.hostPath` to be an **absolute path** on the host. To keep the config portable and reproducible across machines, this repo uses a “template placeholder + generator script” approach to produce the real kind config at runtime.

Generate the kind config first (output: `benchmarks/online_boutique/kind-ob-config.yaml`):

```bash
python benchmarks/online_boutique/scripts/generate_kind_config.py
```

Create the cluster (cluster name `ob`, kubectl context `kind-ob`) using the generated config file:

```bash
kind create cluster --config benchmarks/online_boutique/kind-ob-config.yaml
```

Switch context and verify nodes:

```bash
kubectl config use-context kind-ob
kubectl config current-context
kubectl get nodes -o wide
kubectl get ns
```

---

### 5) Install Istio

Install Istio using the `demo` profile to deploy the control plane and ingress gateway (for sidecar injection and service-level observability).

```bash
istioctl install --set profile=demo -y
```

Verify after installation:

```bash
kubectl get ns
kubectl -n istio-system get pods
kubectl -n istio-system get svc
```

---

### 6) Install the Prometheus monitoring stack

Install the monitoring components via `kube-prometheus-stack`:
- Prometheus
- Grafana
- kube-state-metrics
- node-exporter

Add the Helm repository first:

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update
```

The kind config only sets up registry mirrors for `docker.io` inside kind nodes. `kube-prometheus-stack` also pulls many images from `quay.io`, `ghcr.io`, and `registry.k8s.io`, so we use a safer approach: pull images on the host and then `kind load` them into the cluster.

Download a pinned chart version to a local directory:

```bash
PROM_CHART_VERSION="82.17.0"
PROM_CHART_ROOT="/tmp/ob-kube-prometheus-stack-chart"

rm -rf "$PROM_CHART_ROOT"
mkdir -p "$PROM_CHART_ROOT"

helm pull prometheus-community/kube-prometheus-stack \
  --version "$PROM_CHART_VERSION" \
  --untar \
  --untardir "$PROM_CHART_ROOT"
```

Render manifests from the local chart:

```bash
PROM_CHART_ROOT="/tmp/ob-kube-prometheus-stack-chart"
PROM_CHART="$PROM_CHART_ROOT/kube-prometheus-stack"
PROM_TMP="/tmp/ob-kube-prometheus-stack-rendered.yaml"

helm template monitoring "$PROM_CHART" -n monitoring > "$PROM_TMP"
```

Pull required `kube-prometheus-stack` images on the host and generate an import list:

```bash
PROM_CHART_ROOT="/tmp/ob-kube-prometheus-stack-chart"
PROM_CHART="$PROM_CHART_ROOT/kube-prometheus-stack"
PROM_TMP="/tmp/ob-kube-prometheus-stack-rendered.yaml"
PROM_LOAD_LIST="/tmp/ob-kube-prometheus-stack-images.txt"

IMGS="$(awk '/^[[:space:]]*image:[[:space:]]*/{print $2}' "$PROM_TMP" | sed "s/[\"']//g" | sort -u)"
: > "$PROM_LOAD_LIST"

for IMG in $IMGS; do
  [ -z "$IMG" ] && continue

  CANON="$IMG"
  if [[ "$IMG" == */* ]]; then
    first="${IMG%%/*}"
    if [[ "$first" != *.* && "$first" != *:* && "$first" != "localhost" ]]; then
      CANON="docker.io/$IMG"
    fi
  else
    CANON="docker.io/library/$IMG"
  fi

  if ! docker image inspect "$CANON" >/dev/null 2>&1 && ! docker image inspect "$IMG" >/dev/null 2>&1; then
    if [[ "$CANON" == localhost/* ]]; then
      echo "ERROR: local image missing, please build it first: $CANON" >&2
      exit 1
    fi
    if [[ "$CANON" == docker.io/* ]]; then
      SRC="docker.m.daocloud.io/${CANON#docker.io/}"
    elif [[ "$CANON" == quay.io/* ]]; then
      SRC="quay.m.daocloud.io/${CANON#quay.io/}"
    else
      SRC="docker.gh-proxy.com/$CANON"
    fi
    docker pull "$SRC" || docker pull "$CANON" || docker pull "$IMG" || {
      echo "ERROR: pull failed: $CANON" >&2
      exit 1
    }
    docker tag "$SRC" "$CANON" 2>/dev/null || true
    docker tag "$SRC" "$IMG" 2>/dev/null || true
  fi

  docker tag "$IMG" "$CANON" 2>/dev/null || true
  printf '%s\n' "$CANON" >> "$PROM_LOAD_LIST"
done

sort -u "$PROM_LOAD_LIST" -o "$PROM_LOAD_LIST"
```

Load all `kube-prometheus-stack` images into `kind-ob`:

```bash
PROM_LOAD_LIST="/tmp/ob-kube-prometheus-stack-images.txt"

while IFS= read -r IMG; do
  [ -z "$IMG" ] && continue
  kind load docker-image "$IMG" --name ob
done < "$PROM_LOAD_LIST"
```

Install and wait:

```bash
PROM_CHART_ROOT="/tmp/ob-kube-prometheus-stack-chart"
PROM_CHART="$PROM_CHART_ROOT/kube-prometheus-stack"

helm upgrade --install monitoring "$PROM_CHART" -n monitoring --create-namespace \
  -f benchmarks/online_boutique/manifests/monitoring/kube-prometheus-stack-values.yaml \
  --wait
```

Check monitoring components:

```bash
kubectl -n monitoring get pods
kubectl -n monitoring get pods -w
```

Let Prometheus scrape Istio sidecar and Jaeger metrics:

```bash
kubectl apply -f benchmarks/online_boutique/manifests/monitoring/istio-proxy-podmonitor.yaml
kubectl apply -f benchmarks/online_boutique/manifests/monitoring/jaeger-podmonitor.yaml
```

Verify `PodMonitor` resources:

```bash
kubectl -n monitoring get podmonitor
```

---

### 7) Deploy Online Boutique

The `Online Boutique` manifests reference images not only from `docker.io`, but also from `us-central1-docker.pkg.dev`. We keep the same “host pull + kind load” approach before applying the manifests.

Create the application namespace and enable sidecar injection:

```bash
kubectl create namespace ob || true
kubectl label namespace ob istio-injection=enabled --overwrite
```

Extract the image list referenced by the manifests and generate an import list. If images are not present locally, the following script will pull them and load them into kind:

```bash
OB_LOAD_LIST="/tmp/ob-online-boutique-images.txt"
FILES="benchmarks/online_boutique/microservices-demo/release/kubernetes-manifests.yaml"

IMGS="$(awk '/^[[:space:]]*image:[[:space:]]*/{print $2}' "$FILES" | sed "s/[\"']//g" | sort -u)"
: > "$OB_LOAD_LIST"

for IMG in $IMGS; do
  [ -z "$IMG" ] && continue

  CANON="$IMG"
  if [[ "$IMG" == */* ]]; then
    first="${IMG%%/*}"
    if [[ "$first" != *.* && "$first" != *:* ]]; then
      CANON="docker.io/$IMG"
    fi
  else
    CANON="docker.io/library/$IMG"
  fi

  if ! docker image inspect "$CANON" >/dev/null 2>&1 && ! docker image inspect "$IMG" >/dev/null 2>&1; then
    if [[ "$CANON" == docker.io/* ]]; then
      SRC="docker.m.daocloud.io/${CANON#docker.io/}"
    elif [[ "$CANON" == quay.io/* ]]; then
      SRC="quay.m.daocloud.io/${CANON#quay.io/}"
    else
      SRC="docker.gh-proxy.com/$CANON"
    fi
    docker pull "$SRC" || docker pull "$CANON" || docker pull "$IMG" || {
      echo "WARN: pull failed, skip: $CANON" >&2
      continue
    }
    docker tag "$SRC" "$CANON" 2>/dev/null || true
    docker tag "$SRC" "$IMG" 2>/dev/null || true
  fi

  docker tag "$IMG" "$CANON" 2>/dev/null || true
  printf '%s\n' "$CANON" >> "$OB_LOAD_LIST"
done

sort -u "$OB_LOAD_LIST" -o "$OB_LOAD_LIST"
```

Load all application images into `kind-ob`:

```bash
OB_LOAD_LIST="/tmp/ob-online-boutique-images.txt"

while IFS= read -r IMG; do
  [ -z "$IMG" ] && continue
  kind load docker-image "$IMG" --name ob
done < "$OB_LOAD_LIST"
```

After loading images, apply the manifests and watch pods start up:

```bash
kubectl -n ob apply -f benchmarks/online_boutique/microservices-demo/release/kubernetes-manifests.yaml
```

Check pods:

```bash
kubectl -n ob get pods
kubectl -n ob get pods -w
```

Normally, most pods will show `2/2`, meaning:
- 业务容器
- `istio-proxy` sidecar

Pay attention to:
- `frontend`
- `loadgenerator`
- `checkoutservice`
- `productcatalogservice`
- `cartservice`
- `redis-cart`

If any pods are not `Running`, check recent events:

```bash
kubectl -n ob get events --sort-by=.lastTimestamp | tail -n 30
```

---

### 8) Verify the application is working

Check services:

```bash
kubectl -n ob get svc
```

(Optional) Check the built-in load generator logs:

```bash
kubectl -n ob logs deploy/loadgenerator --tail=80
```

Note: In the provided manifests, `loadgenerator` defaults to `0`, so it is normal to see no logs and no ongoing traffic. If you want a quick smoke check, temporarily scale it to `1` as described in section 9, then revisit the logs.

If you only want to quickly verify the UI can be opened, start a direct port-forward to the frontend service in a **separate terminal** and keep it running. This guide uses `18080` by default to avoid common local port conflicts:

```bash
kubectl -n ob port-forward svc/frontend 18080:80
```

Open in your browser:

```text
http://localhost:18080
```

If the page loads, the application has been deployed successfully.

To prepare an entrypoint for **Locust injection**, this guide uses a fixed **Istio ingress** endpoint (host `18081`). `18080` is only for a quick UI check.

```bash
kubectl -n ob apply -f benchmarks/online_boutique/microservices-demo/istio-manifests/frontend-gateway.yaml
```

Fix the HTTP nodePort of `istio-ingressgateway` to `30081`:

```bash
kubectl -n istio-system patch svc istio-ingressgateway --type='json' \
  -p='[{"op":"replace","path":"/spec/ports/1/nodePort","value":30081}]'
```

Because the kind config already maps host `18081` to kind node `30081`, you do not need `kubectl port-forward` here. Use the following entrypoint for your browser or Locust:

```text
http://localhost:18081
```

The request path is:

```text
Locust / 浏览器 -> localhost:18081 -> kind:30081 -> istio-ingressgateway -> frontend Service -> 多个 frontend Pod
```

---

### 9) Access Prometheus and validate metrics

In **another separate terminal**, port-forward Prometheus and keep it running. This guide uses `19090` by default:

```bash
kubectl -n monitoring port-forward svc/monitoring-kube-prometheus-prometheus 19090:9090
```

Open in your browser:

```text
http://localhost:19090
```

The following are **PromQL queries**. Run them in the Prometheus UI `Expression` input box, not directly in your terminal.

#### 9.1 Minimal validation (recommended for the first deployment)

Since `loadgenerator` defaults to `0`, there is no traffic, so service-level RPS will be `0` and mean latency may be `NaN` (0/0). For the first validation, inject a small amount of traffic temporarily:

```bash
kubectl -n ob scale deploy/loadgenerator --replicas=1
```

1) Deployment ready replicas (verify kube-state-metrics works):

```promql
kube_deployment_status_replicas_available{namespace="ob"}
```

2) Service-level RPS (verify Istio request metrics work):

```promql
sum(rate(istio_requests_total{reporter="destination", destination_workload_namespace="ob"}[1m]))
  by (destination_workload)
```

3) Service-level mean latency (seconds) (verify duration metrics work):

```promql
(
  sum(rate(istio_request_duration_milliseconds_sum{reporter="destination", destination_workload_namespace="ob"}[1m]))
    by (destination_workload)
)
/
(
  sum(rate(istio_request_duration_milliseconds_count{reporter="destination", destination_workload_namespace="ob"}[1m]))
    by (destination_workload)
)
/
1000
```

If all three queries return data, Prometheus + Istio metrics are working and you can continue.

After validation, scale back to `0` to avoid polluting formal injection data:

```bash
kubectl -n ob scale deploy/loadgenerator --replicas=0
```

#### 9.2 Optional queries (for debugging/deeper checks)

Pod CPU：

```promql
sum(rate(container_cpu_usage_seconds_total{namespace="ob", container!="", image!=""}[1m])) by (pod)
```

Pod 内存：

```promql
sum(container_memory_working_set_bytes{namespace="ob", container!="", image!=""}) by (pod)
```

Deployment desired replicas：

```promql
kube_deployment_spec_replicas{namespace="ob"}
```

Service-level 5xx RPS:

```promql
sum(rate(istio_requests_total{reporter="destination", destination_workload_namespace="ob", response_code=~"5.."}[1m]))
  by (destination_workload)
```

Edge-level RPS:

```promql
sum(rate(istio_requests_total{reporter="destination", destination_workload_namespace="ob"}[1m]))
  by (source_workload, destination_workload)
```

---

#### 9.3 Install and enable tracing

Goal: send traces produced by Istio sidecars to the in-cluster `OpenTelemetry Collector` via OTLP, and query them in `Jaeger`.

Install observability components (Jaeger + OTel Collector):

```bash
kubectl create ns observability || true

kubectl -n observability apply -f benchmarks/online_boutique/manifests/observability/jaeger-pvc.yaml
kubectl -n observability apply -f benchmarks/online_boutique/manifests/observability/jaeger.yaml
kubectl -n observability apply -f benchmarks/online_boutique/manifests/observability/otel-collector.yaml
```

Enable Istio tracing (sampling rate 1%):

```bash
kubectl -n istio-system apply -f benchmarks/online_boutique/manifests/istio/telemetry-tracing.yaml
```

Verify Jaeger UI (keep the port-forward running in a separate terminal):

```bash
kubectl -n observability port-forward svc/jaeger-query 16686:16686
```

Open in your browser:

```text
http://127.0.0.1:16686
```
