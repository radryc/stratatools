# lb

L4 load balancer engine with a decoupled registry control plane and TCP proxy data plane.

## Components

- `cmd/registry`: gRPC registry + HTTP `/services` state endpoint
- `cmd/proxy`: state-synced TCP proxy with PROXY protocol v1, passive circuit breaker, and draining
- `cmd/k8s-agent`: Kubernetes Endpoints watcher that writes intent to registry

## Quickstart

```bash
go run ./cmd/registry
# in another shell
go run ./cmd/proxy -registry-url http://127.0.0.1:8081
```

The proxy exports Prometheus-compatible metrics at `:9090/metrics`.

## Edge Mode

The container image defaults to `lb-edge`, which runs a local registry + proxy pair.

Use `LB_BOOTSTRAP` to seed static routes for existing systems:

```bash
LB_BOOTSTRAP="9090=router-a:9090,router-b:9090;8080=router-a:8080,router-b:8080"
```

Format: `<external_port>=host:port[,host:port][;<external_port>=...]`.
