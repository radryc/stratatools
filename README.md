# stratatools

> Part of the **Strata** platform — configuration and templates for the MonoFS + Guardian stack.

The primary CLI tools are Go binaries in the sibling repos: **`guardianctl`** and
**`monofs-admin`**. stratatools provides the templates, partition configs, and a
few remaining Python utilities (`st-image`, `st-aws-setup`).

## Quick Start

```bash
# 1. Check tools, create kind cluster, generate encryption key
cd ../guardian && go run ./cmd/guardianctl/ setup run

# 2. Bootstrap MonoFS + Guardian + LB into the cluster
cd ../guardian && go run ./cmd/guardianctl/ bootstrap init

# 3. Stamp external endpoints into partition configs
cd ../guardian && go run ./cmd/guardianctl/ bootstrap stamp-urls

# 4. Release all partitions
cd ../guardian && go run ./cmd/guardianctl/ release run --all --bump
```

After bootstrap, everything runs through Guardian. Port-forwarding exposes:

| Service | Address | Protocol |
|---------|---------|----------|
| MonoFS HTTP UI | `http://<host>:8080/` | HTTP |
| MonoFS gRPC | `<host>:9090` | gRPC |
| Guardian UI | `http://<host>:8090/` | HTTP |

## Commands

### guardianctl (Go — primary)

```bash
# Bootstrap (only part that can't go through Guardian)
guardianctl bootstrap init                # build images → load into kind → deploy
guardianctl bootstrap build               # build Docker images for bootstrap components
guardianctl bootstrap deploy              # apply K8s templates + wait for readiness
guardianctl bootstrap stop                # scale all deployments to zero
guardianctl bootstrap destroy             # delete namespaces
guardianctl bootstrap status              # show component health
guardianctl bootstrap ports               # start kubectl port-forward
guardianctl bootstrap stamp-urls          # resolve endpoints → stamp partition configs

# Setup
guardianctl setup run                     # clone repos, check tools, create kind cluster

# Release (wraps st-image + guardianctl)
guardianctl release run --all --bump      # release all partitions
guardianctl release run -p doctor --bump --wait  # release one partition

# Image
guardianctl image run build --dir partitions/doctor
guardianctl image run stamp --dir partitions/doctor
```

### monofs-admin (Go)

```bash
monofs-admin ingest --router localhost:9090 --source <url> --ref <branch>
monofs-admin dogfood --router localhost:9090       # ingest all sibling repos
monofs-admin status --router localhost:9090        # cluster health
monofs-admin repos --router localhost:8080          # list ingested repos
```

### Python (remaining)

```bash
uv run st-image build --partition guardian-configs  # build partition images
uv run st-image push --partition guardian-configs   # push/stamp images
uv run st-image stamp --partition guardian-configs
uv run st-aws-setup --aws-profile admin-prod        # IAM Roles Anywhere provisioning
```

## Architecture

```
                  ┌──────────────────────────────────┐
                  │     guardianctl (Go, primary)      │
                  │  bootstrap | setup | release       │
                  └──────────────┬───────────────────┘
                                 │
        After bootstrap, everything goes through Guardian
        (guardianctl partition push → Guardiand → pushers)

  ┌────────────────────────────┐
  │  deploy/bootstrap/          │  K8s manifests (envsubst-compatible)
  │  storage/ — 27 templates    │  MonoFS + MinIO + LB
  │  guardian/ — 10 templates   │  Guardiand + pushers + local registry
  │  bootstrap.yaml            │  Config with defaults
  └────────────────────────────┘

  ┌─────────────────────────────────────┐
  │  src/stratatools/ (Python, secondary) │
  │  image/       — st-image build/push   │
  │  aws_setup/   — st-aws-setup          │
  │  setup/       — legacy helper code    │
  └─────────────────────────────────────┘
```

Templates use `${VAR:-default}` syntax and are rendered with `envsubst`.
The Go bootstrap binary computes all derived values (node addresses, KVS peers,
LB bootstrap strings) and applies them via `kubectl apply`.

## Prerequisites

- **Docker** — running daemon
- **Go** — ≥1.22 (for `guardianctl` and `monofs-admin`)
- **kubectl** — Kubernetes CLI
- **kind** — local Kubernetes clusters
- **uv** — Python package manager (for `st-image` and `st-aws-setup`)
- **envsubst** — template variable substitution (from `gettext` package)

```bash
# Install Python deps (for st-image and st-aws-setup)
uv sync
```

## Bootstrap Flow

1. **Setup**: `guardianctl setup run` — clone sibling repos, check tools, create
   kind cluster named `strata`, generate `MONOFS_ENCRYPTION_KEY` into `../monofs/.env`

2. **Bootstrap init**: `guardianctl bootstrap init` — build Docker images for all
   bootstrap components (MonoFS server, router, fetcher, search, registry, LB edge,
   guardiand, pushers), load them into kind, apply 37 K8s templates via `envsubst`,
   wait for all deployments to become ready

3. **Stamp URLs**: `guardianctl bootstrap stamp-urls` — resolve lb-edge external
   endpoints and write them into partition configs so Guardian and external
   tools can discover MonoFS

4. **Release**: `guardianctl release run --all --bump` — bump version tags, build
   partition images (via `st-image`), stamp immutable refs, push each partition
   to Guardian, optionally reconcile and wait

## Configuration

Defaults are in `deploy/bootstrap/bootstrap.yaml`. Override via environment
variables (same names as the old Python defaults):

| Variable | Default | Purpose |
|----------|---------|---------|
| `MONOFS_NAMESPACE` | `monofs` | Storage namespace |
| `GUARDIAN_NAMESPACE` | `guardian` | Guardian namespace |
| `LB_NAMESPACE` | `lb-edge` | LB edge namespace |
| `MONOFS_SERVER_IMAGE` | `monofs-server:latest` | Server image |
| `GUARDIAN_IMAGE` | `guardian:latest` | Guardiand image |
| `GUARDIAN_AWS_ACCOUNT` | *(empty)* | Enable AWS pusher when set |
| `EXTERNAL_SERVICE_IPS` | *(empty)* | Explicit external IPs for services |
| `GUARDIAN_UI_PORT` | `8090` | Guardian UI port |
| `EXTERNAL_SERVICE_TYPE` | `LoadBalancer` | K8s service type for external |

## Repo Paths

Image builds need the source repos checked out. Each defaults to `<stratatools>/../<name>`:

| Variable | Default | Used by partition |
|----------|---------|-------------------|
| `ST_ROOT` | auto-detected | All — stratatools directory |
| `GUARDIAN_REPO_DIR` | `../guardian` | guardian-configs |
| `MONOFS_REPO_DIR` | `../monofs` | guardian-configs, dev-workspace |
| `DOCTOR_REPO_DIR` | `../doctor` | doctor |
| `KVS_REPO_DIR` | `../kvs` | guardian-configs |
| `K8S_TOP_REPO_DIR` | `../k8s-top` | k8s-top |
| `AGENT_REPO_DIR` | `../agent` | agent |
| `LB_REPO_DIR` | `../lb` | guardian-configs, opentelemetry, lb-agent |
| `LOLIPOP_REPO_DIR` | `../lolipop` | lolipop |

Set any of these to override when repos live elsewhere:

```bash
export LOLIPOP_REPO_DIR=$HOME/aiprojects/lolipop
export DOCTOR_REPO_DIR=$HOME/src/doctor
guardianctl release run --partition lolipop
```

## Rebuilding After Changes

```bash
cd ../guardian && go run ./cmd/guardianctl/ bootstrap init --skip-build
```

This re-applies templates and restarts deployments without rebuilding images. Use
`bootstrap build` first if image sources changed.

## Teardown

```bash
cd ../guardian && go run ./cmd/guardianctl/ bootstrap stop     # scale to zero
cd ../guardian && go run ./cmd/guardianctl/ bootstrap destroy  # delete namespaces
```

## Encryption Key

`MONOFS_ENCRYPTION_KEY` (64 hex chars) is generated by `guardianctl setup run`
into `../monofs/.env`. Never rotate after ingesting data — existing blob
archives become unreadable.

## Local Dev Workspace

After `dev-workspace` partition is released:
- **OpenVSCode** — `http://localhost:8888/`
- **SSH** — `ssh developer@localhost -p 2222`

## ImageBuild Modes

Guardian supports two ImageBuild modes:

| Mode | Property | How it works |
|------|----------|-------------|
| **Source build** | `sourceDir` + `dockerfile` | Guardian stages source, runs `docker build` |
| **Tar push** | `imageTar` + `sourceImage` | Guardian loads pre-built OCI tar, pushes to registry |

Both produce `imageRef` outputs consumable via `${intent.images.outputs.*.imageRef}`.

Example tar-mode:
```yaml
- type: ImageBuild
  name: guardian-build
  properties:
    imageTar: /partitions/guardian-configs/payloads/images/guardian.tar
    sourceImage: guardian:latest
    registry: registry.strata.local:5000
    repository: guardian
```

## Managed Partitions

`guardian-configs`, `opentelemetry`, `k8s-top`, `doctor`, `monitoring`,
`dev-workspace`, `agent`, `lb-agent`, `lolipop`

## Testing

```bash
cd ../guardian && go test ./cmd/guardianctl/           # Go tests
cd stratatools && uv run python -m unittest discover -s tests  # Python tests
```
