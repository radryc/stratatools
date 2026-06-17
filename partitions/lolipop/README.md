# Lolipop — AI application partition

Lolipop is a CUDA-accelerated AI chat stack (frontend, backend, TTS, LoRA
trainer, Wan2GP video generation, ChromaDB, llama.cpp model runners).  It runs
as **Docker containers** managed by Guardian.

This partition includes an **edge-lb registration bridge** so all lolipop
services appear in the unified K8s edge-lb dashboard and are reachable through
the edge-lb proxy — without changing the Docker deployment.

## Quick start

```bash
# After lolipop containers are running, register them with edge-lb:
st-bootstrap edge-register --config partitions/lolipop/edge-register.yaml

# Dry-run first to see what would be registered:
st-bootstrap edge-register --config partitions/lolipop/edge-register.yaml --dry-run

# Verify they appear:
st-bootstrap edge-status
curl -s http://<host>:18081/services | jq .

# Deregister when shutting down:
st-bootstrap edge-deregister --config partitions/lolipop/edge-register.yaml
```

## Architecture

```
 Docker host
 ┌──────────────────────────────────────────────────────┐
 │  lolipop-frontend  lolipop-backend  lolipop-wangp    │
 │  (port 3000)       (port 8090)      (port 7860)      │
 │       │                  │                 │          │
 │       │   st-bootstrap edge-register --config ...     │
 │       │   (matches containers by image name)          │
 │       │        │                          │           │
 └───────┼────────┼──────────────────────────┼───────────┘
         │        │                          │
         │        │ grpcurl RegisterService  │
         ▼        ▼                          ▼
     ┌──────────────────────────────────────────┐
     │      K8s edge-lb (hostNetwork)            │
     │  gRPC :15051    HTTP /services :18081     │
     │  ┌──────────┐    ┌──────────────────┐    │
     │  │ Registry │◄───│ lb-k8s-agent     │    │
     │  └────┬─────┘    │ (K8s Endpoints)  │    │
     │       │          └──────────────────┘    │
     │  ┌────▼─────┐                            │
     │  │  Proxy   │  opens TCP listeners       │
     │  └──────────┘  for all registered svcs   │
     └──────────────────────────────────────────┘
                    │
                    ▼
    External access via host IP:port
    Dashboard: http://<host>:18081/
```

## Two registration modes

### Mode 1: Config file (no container changes needed)

The file `edge-register.yaml` maps Docker image names to edge-lb services.
Used with the `--config` flag — perfect for existing containers that
haven't been labelled.

### Mode 2: Container labels (preferred for new services)

Set labels on containers at creation time.  The `edge-register` command
discovers them automatically, no config file needed.

| Label | Value |
|-------|-------|
| `guardian.intent/expose` | `"true"` |
| `guardian.intent/service-name` | e.g. `"lolipop-frontend"` |
| `guardian.intent/account` | e.g. `"lolipop"` |
| `guardian.intent/port` | e.g. `"3000"` |

**docker run:**
```bash
docker run -d \
  --label guardian.intent/expose=true \
  --label guardian.intent/service-name=lolipop-frontend \
  --label guardian.intent/port=3000 \
  --label guardian.intent/account=lolipop \
  lolipop-frontend:latest
```

**docker-compose:**
```yaml
services:
  lolipop-frontend:
    image: lolipop-frontend:latest
    labels:
      guardian.intent/expose: "true"
      guardian.intent/service-name: "lolipop-frontend"
      guardian.intent/port: "3000"
      guardian.intent/account: "lolipop"
```

## Registered services

| Service | Container port | Image match |
|---------|---------------|-------------|
| `lolipop-frontend` | 3000 | `lolipop-frontend` |
| `lolipop-backend` | 8090 | `lolipop-backend` |
| `lolipop-chromadb` | 8000 | `chromadb/chroma` |
| `lolipop-model-runner` | 12434 | `lolipop-model-runner`* |
| `lolipop-vl-runner` | 12435 | `lolipop-vl-runner`* |
| `lolipop-wangp` | 7860 | `lolipop-wangp` |
| `lolipop-wangp-bridge` | 7861 | `lolipop-wangp` |

*Model runners use the `ghcr.io/ggml-org/llama.cpp:server-cuda` image
on Docker.  Use the container name or set labels if the image repo
doesn't match.

## Files

| File | Purpose |
|------|---------|
| `config.yaml` | Partition metadata |
| `intents/10-core.yaml` | Docker core services (chromadb, backend, frontend) |
| `intents/20-model-runners.yaml` | GPU model runners (llama.cpp, wangp) |
| `edge-register.yaml` | Service-to-image mappings for edge-lb registration |
| `../src/stratatools/edge_register.py` | Generic Docker→edge-lb bridge |

## Prerequisites

- K8s edge-lb deployed (`lb-edge` namespace, gRPC on port 15051)
- `grpcurl` on PATH (`go install github.com/fullstorydev/grpcurl/cmd/grpcurl@latest`)
- `docker` CLI on PATH
- Lolipop Docker containers running
