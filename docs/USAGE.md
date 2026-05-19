# Stratatools Usage

`stratatools` is the single-repo entry point for bringing up the local Strata
platform.

The normal flow is:

1. Clone `stratatools`.
2. Run `st-setup` to clone the sibling repositories beside it.
3. Run `st-bootstrap` to build host CLIs, build bootstrap images, and deploy
   bootstrap MonoFS + Guardian.
4. Run `st-release --all` to build, distribute, stamp, and reconcile the
   managed partitions.

## Prerequisites

You need these host tools:

- `git`
- `docker` with a running daemon
- `kubectl`
- `go >= 1.22`
- `python >= 3.11`
- `make`
- `uv` recommended

You also need a reachable Kubernetes cluster before `st-setup` and
`st-bootstrap deploy` can succeed. Supported local options are:

- Docker Desktop Kubernetes
- `kind`
- `minikube`
- a remote cluster via `KUBECONFIG`

`st-setup` validates the toolchain and cluster and prints install hints when
something is missing.

## Fresh Checkout

Clone only `stratatools`, then install the Python environment:

```bash
git clone <your-stratatools-repo-url>
cd stratatools
uv sync
```

If you prefer a plain virtualenv instead of `uv`, install the package in
editable mode:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Full Bring-Up

This is the shortest end-to-end flow for a fresh machine that already has the
host prerequisites and a running Kubernetes cluster:

```bash
uv run st-setup
uv run st-bootstrap deploy
uv run st-release --all --wait
```

What each step does:

1. `uv run st-setup`
   Clones the sibling repositories beside `stratatools` if they do not already
   exist, then verifies Docker, kubectl, Go, Python, and cluster reachability.
2. `uv run st-bootstrap deploy`
   Builds `guardianctl`, `monofs-client`, `monofs-session`, and
   `monofs-search` into `~/bin`, deploys bootstrap MonoFS storage, deploys
   bootstrap Guardian, installs `metrics-server`, and applies the bootstrap
   RBAC manifests used by OpenTelemetry, `k8s-top`, and the dev workspace.
3. `uv run st-release --all --wait`
   Builds local partition images where needed, distributes them, stamps image
   references into the partition YAML, pushes the partitions with
   `guardianctl`, reconciles them, and waits for convergence.

When `dev-workspace` is part of the released set, the intended localhost
access points are:

- OpenVSCode: `http://localhost:8888/`
- SSH: `ssh monofs@localhost -p 2222`

SSH access also requires the `ssh-authorized-keys` config in the
`dev-workspace` partition to contain your public key.

## What Gets Built

Bootstrap build output:

- `guardianctl`
- `monofs-client`
- `monofs-session`
- `monofs-search`
- `monofs-server`
- `monofs-router`
- `monofs-fetcher`
- `monofs-search`
- `guardian`
- `guardian-pusher-k8s`

Release pipeline for `st-release --all` covers these managed partitions:

- `guardian-configs`
- `opentelemetry`
- `k8s-top`
- `doctor`
- `monitoring`
- `dev-workspace`

That means the full flow covers:

- MonoFS bootstrap images and host CLIs
- Guardian bootstrap images and `guardianctl`
- Doctor partition images
- every repo-managed partition known to `stratatools`

## Distribution Mode

`st-image` and `st-release` inspect the current kubectl context:

- on `docker-desktop` and `kind-*`, images are cluster-loaded into the nodes
- on other contexts, images are tagged and pushed to the registry given by
  `--registry`

The default registry is `localhost:5000` when cluster-load mode is not active.

Examples:

```bash
uv run st-release --all --registry registry.internal:5000 --wait
uv run st-image list
uv run st-image build --partition doctor
```

## URL Stamping

Use `st-bootstrap stamp-urls` when the host-reachable Guardian or Doctor
endpoints change.

Typical usage:

```bash
# after bootstrap Guardian gets an external address
uv run st-bootstrap stamp-urls

# after the doctor partition has been released and doctor-query gets an external address
uv run st-bootstrap stamp-urls
```

This updates the checked-in partition YAML under:

- `partitions/guardian-configs`
- `partitions/doctor`

## Useful Day-2 Commands

Rebuild bootstrap binaries and images without deploying:

```bash
uv run st-bootstrap build
```

Rebuild bootstrap images and restart the bootstrap workloads:

```bash
uv run st-bootstrap rollout
```

Release a single partition:

```bash
uv run st-release --partition doctor --wait
uv run st-release --partition monitoring --wait
```

Ingest the whole Strata repo set into MonoFS using each local checkout's
`origin` remote and currently checked out ref:

```bash
uv run st-dogfood --router localhost:9090
```

`st-dogfood` discovers `stratatools` plus the sibling repositories cloned by
`st-setup`. If `monofs-admin` is missing, it builds it into `~/bin` first.
Use `--router` or `MONOFS_ROUTER` when the MonoFS router is reachable on a
different address.

Inspect known partitions:

```bash
uv run st-image list
```

Dry-run the main workflows:

```bash
uv run st-setup --dry-run
uv run st-bootstrap build --dry-run
uv run st-release --all --dry-run
```

## Paths And Overrides

By default, `st-setup` clones sibling repositories into the parent directory
that contains `stratatools`.

Example layout after `st-setup`:

```text
workspace/
  stratatools/
  guardian/
  doctor/
  monofs/
  kvs/
  agent/
  packager/
  cfg/
```

Useful overrides:

- `--parent-dir` on `st-setup`
- `STRATATOOLS_BIN_DIR`
- `GUARDIAN_REPO_DIR`
- `MONOFS_REPO_DIR`
- `DOCTOR_REPO_DIR`
- `KVS_REPO_DIR`
- `GUARDIANCTL_BIN`

## Shutdown

Stop the bootstrap workloads:

```bash
uv run st-bootstrap stop
```

Delete the bootstrap MonoFS and Guardian namespaces:

```bash
uv run st-bootstrap destroy
```

This removes the bootstrap namespaces only. Released partitions and retained
cluster storage still need separate cleanup if you want a full reset.

## Validation Notes

The command flow documented here was matched against the current Python CLI in
`src/stratatools/` and validated in dry-run mode during migration work.
