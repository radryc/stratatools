# Monitoring partition

This bundle mirrors the Guardian-manageable portion of the monitoring setup:

- Grafana config, dashboards, workload, internal service, and external edge service.
- Doctor-backed Grafana datasources for Prometheus-compatible metrics, Loki-compatible logs, and Zipkin-compatible traces.
- The same dev defaults: `admin/admin`, anonymous Grafana Editor access, Explore enabled, and ephemeral storage.

Current dashboard set

- `System / System - Live Topology`: a true Grafana node graph for Guardian, MonoFS, KVS, and Doctor, backed by live Prometheus edge queries with log and trace handoff panels underneath.
- `System / K8s - Cluster Resource Watch`: pod and container CPU/memory from the standalone `k8s-top` exporter, plus release markers, exporter logs, and scrape traces.
- `System / K8s - Namespace And Pod Stats`: a `k8s-top` board focused on namespace density, scrape cadence, the hottest pods and containers, and release overlays.
- `Guardian Rollouts / Guardian Rollouts - Partition Resource Lens`: partition CPU and memory from `k8s-top`, aligned with Guardian result logs and partition-level apply activity.
- `Guardian Rollouts / Guardian Rollouts - Intent Correlation`: Guardian result logs filtered by partition and intent regex, with pod CPU and memory matched inside the selected partition namespace.
- `MonoFS / MonoFS - Comprehensive Health`: router, server, and KVS health with MonoFS logs and trace-linked logs.
- `Guardian / Guardian - Comprehensive Health`: control-loop health, partition and intent status, rollout-task telemetry, and release overlays.
- `Doctor / Doctor - Comprehensive Health`: `doctor-ingest` and `doctor-query` liveness plus ingest backlog and accepted-signal throughput.

The rollout dashboards only use Guardian-backed release signals. Current Guardian metrics plus `k8s-top` do not expose per-partition or per-intent network usage, so those dashboards intentionally stop at CPU, memory, and Guardian rollout activity.

The Doctor dashboard expects the current Doctor code in this repo, which now exports `doctor_component_up`, `doctor_ingest_buffer_*`, and `doctor_ingest_*_accepted` metrics over OTEL.

Local registry image cache

The monitoring workloads are stamped through Kubernetes payload files under `payloads/`, and the matching inline partition image fields are kept in sync, so the cluster pulls immutable image references resolved by `st-image` (sideloaded in cluster-load mode, otherwise from the configured registry) instead of Docker Hub.

Build local images, distribute them, and stamp the payload files before reconciling the partition. The standard one-shot is:

```bash
st-release --partition monitoring
```

Or drive the lower-level steps directly:

```bash
st-image build --partition monitoring
st-image push  --partition monitoring
st-image stamp --partition monitoring
```

Bootstrap only brings up MonoFS and Guardian. Deploy the remaining partitions with `guardianctl`, for example:

```bash
guardianctl partition push --dir ./partitions/opentelemetry
guardianctl partition reconcile --partition opentelemetry
guardianctl partition wait --partition opentelemetry
guardianctl partition push --dir ./partitions/doctor
guardianctl partition reconcile --partition doctor
guardianctl partition wait --partition doctor
guardianctl partition push --dir ./partitions/monitoring
guardianctl partition reconcile --partition monitoring
guardianctl partition wait --partition monitoring
```

Bootstrap prerequisite

Guardian's current Kubernetes model does not own `ServiceAccount`, `ClusterRole`, `ClusterRoleBinding`, or `serviceAccountName` on deployments. Because of that, the OpenTelemetry collector intent runs on the `otel` namespace default service account and needs an out-of-band RBAC binding for `kubernetes_sd_configs` jobs.

`st-bootstrap deploy` and `st-bootstrap rollout` now apply [../opentelemetry/collector-prometheus-rbac-default-sa.yaml](../opentelemetry/collector-prometheus-rbac-default-sa.yaml) automatically.

Bootstrap also installs `metrics-server` and patches it with `--kubelet-insecure-tls`, because the `k8s-top` partition reads the Kubernetes metrics API instead of scraping kubelets directly.

If you skip bootstrap and deploy the partitions manually, apply it yourself before or right after the first reconcile:

```bash
kubectl apply -f partitions/opentelemetry/collector-prometheus-rbac-default-sa.yaml
```

Without that RBAC manifest, the collector still starts, but the Kubernetes service-discovery scrape jobs that feed metrics into Doctor will not be authorized to watch pods, services, nodes, and ingresses.
