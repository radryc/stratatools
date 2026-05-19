# stratatools

> Part of the **Strata** platform.

Toolkit for building and deploying the whole ainfra (Strata) system. Provides:

- `st-setup`     — clone required repos and verify kubernetes/docker readiness
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
   sibling repos beside `stratatools`.
2. `st-bootstrap` builds the host CLIs, builds the bootstrap MonoFS and
   Guardian images, and deploys the bootstrap control plane.
3. `st-release --all` builds, distributes, stamps, and reconciles all managed
   partitions.

`st-bootstrap build|deploy|rollout` also builds these host binaries into
`~/bin` by default:

- `guardianctl`
- `monofs-client`
- `monofs-session`
- `monofs-search`

## Quickstart

```bash
# clone just this repo
git clone <your-stratatools-repo-url>
cd stratatools

# install the Python environment
uv sync

# clone sibling repos and verify prerequisites
uv run st-setup

# build bootstrap CLIs/images and deploy MonoFS + Guardian
uv run st-bootstrap deploy

# build, distribute, stamp, and reconcile every managed partition
uv run st-release --all
```

After the `dev-workspace` partition is released locally, these loopback entry
points are intended to be available:

- OpenVSCode: `http://localhost:8888/`
- SSH into the dev workspace: `ssh monofs@localhost -p 2222`

SSH access also requires a public key to be configured in the
`ssh-authorized-keys` config for the `dev-workspace` partition.

If the external Guardian or Doctor endpoint changes, refresh the stamped
partition config:

```bash
uv run st-bootstrap stamp-urls
```

See [docs/USAGE.md](docs/USAGE.md) for detailed usage.
