# stratatools

> Part of the **Strata** platform.

Toolkit for building and deploying the whole ainfra (Strata) system. Provides:

- `st-setup`     — clone required repos and verify kubernetes/docker readiness
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
   sibling repos beside `stratatools`, then ensures a shared
   `../monofs/.env` with `MONOFS_ENCRYPTION_KEY`.
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
`GUARDIAN_AWS_ACCOUNT` is set (for example in `bootstrap.local.env`).

Use `--bump` for normal releases that rebuild or restamp images. Without it,
`st-release` will refuse to change already stamped immutable image refs.
Add `--wait` when you also want the command to block for convergence.

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

# optional: set up AWS prerequisites and local private bootstrap overrides
# uv run st-aws-setup --aws-profile admin-prod --aws-default-region us-east-1
# cp bootstrap.local.env.example bootstrap.local.env

# build bootstrap CLIs/images and deploy MonoFS + Guardian
uv run st-bootstrap deploy

# build, distribute, stamp, and reconcile every managed partition
uv run st-release --all --bump
```

## Clean-Cluster Test

For a brand-new or intentionally reset cluster, this is the shortest
end-to-end validation flow:

```bash
uv run st-setup
uv run st-bootstrap deploy
uv run st-bootstrap stamp-urls
uv run st-release --all --bump
uv run st-dogfood --router localhost:9090
```

This covers:

- shared `MONOFS_ENCRYPTION_KEY` creation in `../monofs/.env`
- bootstrap MonoFS + Guardian deployment
- release of every managed partition
- ingestion of the default local Strata repositories into MonoFS

If you want parallel AWS + Kubernetes pushers during bootstrap, set
`GUARDIAN_AWS_ACCOUNT` in `bootstrap.local.env` before `st-bootstrap deploy`.

Keep the same `MONOFS_ENCRYPTION_KEY` once MonoFS has ingested repositories.
Rotating it after ingestion can make existing blob archives unreadable until
they are re-ingested.

`st-dogfood` excludes `agent` from the default ingestion set.

After the `dev-workspace` partition is released locally, these loopback entry
points are intended to be available:

- OpenVSCode: `http://localhost:8888/`
- SSH into the dev workspace: `ssh developer@localhost -p 2222`

SSH access also requires a public key to be configured in the
`ssh-authorized-keys` config for the `dev-workspace` partition.

Local `st-dogfood` ingest also requires the MonoFS router and fetchers to
share `MONOFS_ENCRYPTION_KEY`. `st-setup` now seeds that key into
`../monofs/.env`, and both bootstrap and dogfood reuse it. `st-dogfood` does
not create or rotate the key; when `--router` points at localhost and the
active runtime is missing the wiring, it only repairs the detected local or
bootstrap MonoFS runtime to use the existing seeded key.

For stratatools commands, `../monofs/.env` is the canonical local key source.
An ambient shell `MONOFS_ENCRYPTION_KEY` is only used to seed that file when it
does not exist yet.

`st-bootstrap deploy` now stamps the current Guardian UI and host-reachable
MonoFS client endpoint into the checked-in partition config automatically.
If those external endpoints later change, refresh the stamped partition config:

```bash
uv run st-bootstrap stamp-urls
```

See [docs/USAGE.md](docs/USAGE.md) for detailed usage.
