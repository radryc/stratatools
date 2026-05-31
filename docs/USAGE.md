# Stratatools Usage

`stratatools` is the single-repo entry point for bringing up the local Strata
platform.

The normal flow is:

1. Clone `stratatools`.
2. Run `st-setup` to clone the sibling repositories beside it.
3. Run `st-bootstrap` to build host CLIs, build bootstrap images, and deploy
   bootstrap MonoFS + Guardian.
4. Run `st-release --all --bump` to build, distribute, stamp, and reconcile the
   managed partitions.

For AWS local deploy runners that need IAM Roles Anywhere credentials for
CloudFormation-based deployments, use `st-aws-setup` to provision/update:

- Roles Anywhere trust anchor
- CloudFormation execution role
- local deployer role + policy attachments
- Roles Anywhere profile

Example:

```bash
uv run st-aws-setup --root-ca-cert ~/local-pki/rootCA.pem --region us-east-1
uv run st-aws-setup --aws-profile admin-prod --aws-default-region us-east-1
```

To keep private AWS account data out of git, put bootstrap overrides in a
local env file:

```bash
cd stratatools
cp bootstrap.local.env.example bootstrap.local.env
# edit bootstrap.local.env with your account/role values
uv run st-bootstrap deploy
```

`bootstrap.local.env` is git-ignored, and values from that file are loaded by
`st-bootstrap` commands.

Use `--bump` for normal releases that rebuild or restamp images. Without it,
`st-release` will refuse to change already stamped immutable image refs.
Add `--wait` when you also want the command to block for convergence.

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
uv run st-release --all --bump --wait
```

What each step does:

1. `uv run st-setup`
   Clones the sibling repositories beside `stratatools` if they do not already
   exist, ensures `../monofs/.env` contains a shared
   `MONOFS_ENCRYPTION_KEY`, then verifies Docker, kubectl, Go, Python, and
   cluster reachability.
2. `uv run st-bootstrap deploy`
   Builds `guardianctl`, `monofs-client`, `monofs-session`, and
   `monofs-search` into `~/bin`, deploys bootstrap MonoFS storage, deploys
   bootstrap Guardian, installs `metrics-server`, and applies the bootstrap
   RBAC manifests used by OpenTelemetry, `k8s-top`, and the dev workspace,
   reusing the same `MONOFS_ENCRYPTION_KEY` from `../monofs/.env`, then stamps
   the current Guardian UI and host-reachable MonoFS client endpoint into the
   checked-in partition config for later releases.

   Add `--dns` when you also want bootstrap to build `devdns`/`devdnsctl`,
   start local devdns, and keep declared `DevDNSRoute` assets synced through
   local `kubectl port-forward` processes.
3. `uv run st-release --all --bump --wait`
   Builds local partition images where needed, distributes them, stamps image
   references into the partition YAML, pushes the partitions with
   `guardianctl`, reconciles them, and waits for convergence.

When `dev-workspace` is part of the released set, the intended localhost
access points are:

- OpenVSCode: `http://localhost:8888/`
- SSH: `ssh developer@localhost -p 2222`

When the Doctor partition is released and local devdns is active, the intended
browser entry point is:

- Doctor query UI: `http://doctor.strata/`

For LAN-wide wildcard `.strata` DNS, run devdns on one reachable host, set
`DEVDNS_SERVER_IP` to that host LAN IP, set `DEVDNS_DNS_ADDR=0.0.0.0:53`, and
point your router or clients at that host as their DNS server.

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

Use `st-bootstrap stamp-urls` when the host-reachable Guardian UI, Doctor
query, or Guardian MonoFS client endpoint changes.

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

Build bootstrap binaries/images and the local devdns binaries:

```bash
uv run st-bootstrap build --dns
```

Rebuild bootstrap images and restart the bootstrap workloads:

```bash
uv run st-bootstrap rollout
```

Restart bootstrap workloads and resync local devdns routes:

```bash
uv run st-bootstrap rollout --dns
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
different address. When the router is local, `st-dogfood` expects the MonoFS
router and fetchers to share `MONOFS_ENCRYPTION_KEY`; `st-setup` now seeds that
key into `../monofs/.env`, bootstrap reuses it, and `st-dogfood` repairs a
detected local or bootstrap MonoFS runtime to use that existing key if the
active router is still missing the key wiring. `st-dogfood` does not create or
rotate the key. The default dogfood set excludes `agent`.

For stratatools commands, `../monofs/.env` is the canonical local key source.
An ambient shell `MONOFS_ENCRYPTION_KEY` is only used to seed that file when it
does not exist yet.

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
