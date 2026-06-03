# stratatools

> Part of the **Strata** platform.

Toolkit for building and deploying the whole ainfra (Strata) system. Provides:

- `st-setup`     — clone required repos, verify kubernetes/docker readiness, and create a local kind cluster when none is reachable
- `st-aws-setup` — provision IAM Roles Anywhere + CloudFormation deploy IAM roles for AWS pusher runners
- `st-bootstrap` — phase 1 (storage) + phase 2 (Guardian) cluster bootstrap and install local CLIs into `~/bin`
- `st-image`     — build / push / stamp partition images
- `st-release`   — one-shot release pipeline (build → push → stamp → guardianctl)

## Install

```bash
uv sync
```

## What It Does

The intended workflow is to clone only `stratatools`, then let the CLI pull in
the sibling Strata repositories and drive the platform bring-up in a few
commands:

1. `st-setup` clones `guardian`, `doctor`, `monofs`, `kvs`, and the other
   sibling repos beside `stratatools`, ensures a shared `../monofs/.env` with
   `MONOFS_ENCRYPTION_KEY`, and auto-creates or reuses a local `kind` cluster
   named `strata` with three workers when no cluster is reachable.
2. `st-bootstrap` builds the host CLIs, builds the bootstrap MonoFS and
   Guardian images, and deploys the bootstrap control plane using that same
   MonoFS encryption key.
3. `st-release --all --bump` builds, distributes, stamps, and reconciles all
   managed partitions.

### Optional AWS Setup

If you run the AWS pusher flow, bootstrap AWS prerequisites with:

```bash
uv run st-aws-setup --aws-profile admin-prod --aws-default-region us-east-1
```

`admin-prod` is an AWS CLI profile name from `~/.aws/config`.
In most org setups this is an AWS IAM Identity Center (SSO) profile.

Typical setup:

```bash
aws configure sso --profile admin-prod
aws sso login --profile admin-prod
```

Then run `st-aws-setup` with that profile.
If you use environment credentials instead of profiles, you can omit
`--aws-profile` and keep only `--aws-default-region`.

AWS CLI SSO/profile docs:
https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sso.html

To keep private AWS account details out of git, put AWS bootstrap overrides in
`bootstrap.local.env`:

```bash
cp bootstrap.local.env.example bootstrap.local.env
# edit bootstrap.local.env
uv run st-bootstrap deploy
```

`bootstrap.local.env` is git-ignored and is auto-loaded by `st-bootstrap`
commands (`build`, `deploy`, `rollout`, `stamp-urls`).

AWS pusher deployment is opt-in: it is deployed only when
# stratatools

> Part of the **Strata** platform.

Toolkit for building and deploying the whole ainfra (Strata) system.

## Commands

| Command | Description |
|---|---|
| `st-setup` | Clone sibling repos, check prerequisites, auto-create a kind cluster |
| `st-bootstrap build` | Build local CLI binaries + MonoFS/Guardian container images |
| `st-bootstrap deploy` | Build + deploy MonoFS storage and Guardian control plane |
| `st-bootstrap rollout` | Rebuild images and restart existing deployments |
| `st-bootstrap stamp-urls` | Resolve live endpoints and stamp them into partition configs |
| `st-bootstrap stop` | Scale all bootstrap deployments to zero |
| `st-bootstrap destroy` | Delete Guardian and storage namespaces |
| `st-release` | Build → push → stamp → push to Guardian for one or more partitions |
| `st-image` | Build / push / stamp partition images individually |
| `st-dogfood` | Ingest local Strata repositories into the running MonoFS cluster |
| `st-aws-setup` | Provision IAM Roles Anywhere + CloudFormation roles for AWS pusher |

## Prerequisites

- Docker
- Go (for building host binaries)
- `kubectl` + `kind` (for local Kubernetes cluster)
- `uv` (Python package manager)

Install the Python environment:

```bash
uv sync
```

`st-bootstrap build|deploy|rollout` also builds and installs these host binaries
into `~/bin`:

- `guardianctl`
- `monofs-client`
- `monofs-session`
- `monofs-search`

## Step-by-Step Deployment

### 1. Clone sibling repos and verify prerequisites

```bash
uv run st-setup
```

This clones `guardian`, `doctor`, `monofs`, `kvs`, `k8s-top`, `agent`,
`packager`, and `cfg` as siblings of the `stratatools` directory, checks that
Docker / Go / kubectl / kind are installed, seeds `../monofs/.env` with a new
`MONOFS_ENCRYPTION_KEY` if one does not exist, and auto-creates a local kind
cluster named `strata` (3 workers) when no Kubernetes cluster is reachable.

### 2. (Optional) Set local overrides

```bash
cp bootstrap.local.env.example bootstrap.local.env
# edit bootstrap.local.env
```

`bootstrap.local.env` is git-ignored and auto-loaded by every `st-bootstrap`
command. Common overrides:

| Variable | Purpose |
|---|---|
| `GUARDIAN_AWS_ACCOUNT` | Enable AWS pusher deployment (opt-in) |
| `MONOFS_PORT_FORWARD_ADDRESS` | Bind address for the managed port-forward (default: `0.0.0.0`; set to `127.0.0.1` for loopback-only) |
| `EXTERNAL_SERVICE_IP` / `EXTERNAL_SERVICE_IPS` | Publish bootstrap Services on one or more explicit host IPs when no LoadBalancer controller is available |
| `GUARDIAN_UI_PORT` | Guardian UI port (default: `8090`) |
| `EXTERNAL_SERVICE_TYPE` | Kubernetes service type for external services (default: `LoadBalancer`) |

### 3. (Optional) Set up AWS prerequisites

Skip this step if you only need a local Kubernetes deployment.

```bash
# authenticate with AWS SSO
aws configure sso --profile admin-prod
aws sso login --profile admin-prod

# provision IAM Roles Anywhere and CloudFormation roles
uv run st-aws-setup --aws-profile admin-prod --aws-default-region us-east-1
```

If you use environment credentials instead of a profile, omit `--aws-profile`.

### 4. Deploy the bootstrap control plane

```bash
uv run st-bootstrap deploy
```

This builds the host CLIs (`guardianctl`, `monofs-*`) into `~/bin`, builds the
MonoFS and Guardian container images, loads them into the kind cluster, deploys
MonoFS storage and the Guardian control plane, and stamps external endpoints
into the partition configs.

After deploy completes, all services are accessible through the single lb-edge
(`monofs-haproxy`) managed port-forward, which binds `0.0.0.0` so it is
reachable from both `localhost` and the host's LAN IP (e.g. `172.21.63.46` on
WSL2 `eth0`):

| Service | URL | Protocol |
|---|---|---|
| MonoFS HTTP UI / API | `http://<host>:8080/` | HTTP |
| MonoFS gRPC API | `<host>:9090` | gRPC |
| Guardian UI | `http://<host>:8090/` | HTTP |

`stamp-urls` automatically detects the host IP and writes it into the
`guardian-configs` and `doctor` partition configs so `guardianctl` and
in-cluster services resolve the correct endpoint without further manual
configuration.

If your cluster has no LoadBalancer controller, set `EXTERNAL_SERVICE_IP` (or
`EXTERNAL_SERVICE_IPS`) in `bootstrap.local.env` to an existing host IP like
`172.21.63.46`. Bootstrap will render that address into the external Services
so `kubectl get svc` shows it in `EXTERNAL-IP`, and stamped URLs will prefer it
over NodePorts.

To override the bind address set `MONOFS_PORT_FORWARD_ADDRESS` in
`bootstrap.local.env` (e.g. `127.0.0.1` for loopback-only).

### 5. Release partitions

```bash
uv run st-release --all --bump
```

This builds, pushes (or cluster-loads on kind), stamps, and pushes each
partition to Guardian. Use `--wait` to block until all partitions converge.

Managed partitions: `agent`, `dev-workspace`, `doctor`, `guardian-configs`,
`k8s-top`, `lolipop`, `monitoring`, `opentelemetry`.

To release a subset:

```bash
uv run st-release -p doctor -p monitoring --bump --wait
```

> **Note:** `lolipop` requires a CUDA-capable GPU and custom images not built
> by stratatools. Skip it on CPU-only or kind clusters by releasing other
> partitions explicitly with `-p`.

### 6. Ingest repositories into MonoFS (optional)

```bash
uv run st-dogfood --router localhost:9090
```

Ingests the local Strata repositories into the running MonoFS cluster.
`MONOFS_ENCRYPTION_KEY` must match the key used at bootstrap (it is
automatically read from `../monofs/.env`).

## Rebuilding After Changes

To rebuild images and roll out without redeploying the full stack:

```bash
uv run st-bootstrap rollout
```

To refresh external endpoint stamps in partition configs:

```bash
uv run st-bootstrap stamp-urls
```

## Teardown

```bash
# scale everything to zero (keeps cluster state)
uv run st-bootstrap stop

# delete all namespaces and resources
uv run st-bootstrap destroy
```

## Encryption Key

`MONOFS_ENCRYPTION_KEY` is seeded into `../monofs/.env` by `st-setup` and
reused by all subsequent commands. Do not rotate this key after MonoFS has
ingested data — existing blob archives will become unreadable until they are
re-ingested.

## Local Dev Workspace

After `dev-workspace` is released, these entry points become available:

- **OpenVSCode** — `http://localhost:8888/`
- **SSH** — `ssh developer@localhost -p 2222`

SSH access requires a public key in the `ssh-authorized-keys` config for the
`dev-workspace` partition.

See [docs/USAGE.md](docs/USAGE.md) for further detail.
