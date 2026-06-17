---
name: strata-monofs
description: >
  MonoFS development flow — build, test, debug, and release the MonoFS
  FUSE-backed virtual filesystem. Covers the monofs-client (FUSE mount),
  monofs-server (router), monofs-admin (CLI), and monofs-session (snapshot
  tool). Use when working in the monofs sibling repo, debugging FUSE mount
  issues, building monofs images, or changing the MonoFS protocol.
---

# MonoFS Development Flow

## Repository layout

The `monofs` repo is a sibling of `stratatools` at `../monofs`.
It contains Go source with Bazel build system.

```
monofs/
  cmd/
    monofs-client/     # FUSE daemon (mounts virtual monorepo)
    monofs-server/     # Router/gateway
    monofs-admin/      # CLI for cache/metadata operations
    monofs-session/    # Snapshot write sessions (aliased as `mfs`)
  pkg/                 # Shared libraries
  proto/               # gRPC protocol definitions
```

## Building

```bash
cd ../monofs
bazel build //cmd/...                 # Build all binaries
bazel test //...                       # Run all tests
```

Binary outputs land in `bazel-bin/cmd/<name>/<name>_/<name>`.

## Docker image build (via stratatools)

```bash
# Build the dev-workspace image (includes monofs-client)
uv run st-image build --partition dev-workspace

# Build just the monofs-client base image
docker build -t monofs-client:dev-base --target client ../monofs
```

The `monofs-client:dev-base` image is a build dependency for
`dev-workspace-opencode`.

## Running monofs-client

The client FUSE-mounts a virtual monorepo backed by the monofs-server:

```bash
monofs-client \
  --router=monofs-external.storage-k8s.svc.cluster.local:9090 \
  --mount=/mnt/monofs \
  --cache=/var/cache/monofs \
  --virtual-monorepo \
  --writable \
  --overlay=/home/developer/.monofs/overlay \
  --debug
```

Key flags:
- `--virtual-monorepo` — present a virtual directory tree from all blobs
- `--writable` — allow write operations (requires `--overlay`)
- `--overlay` — directory to store uncommitted writes

## Write sessions (mfs)

```bash
mfs setup --mount /mnt/monofs   # Initialize session env
mfs start                        # Begin write session
mfs status                       # Show pending changes
mfs commit                       # Commit changes to blob store
```

## Debugging FUSE issues

1. Check if mount is ready:
   ```bash
   mountpoint -q /mnt/monofs
   ```

2. Check client logs:
   ```bash
   tail -f /var/log/monofs-client.log
   tail -f /var/log/monofs-client.json  # structured JSON log
   ```

3. Verify router connectivity:
   ```bash
   nc -zv monofs-external.storage-k8s.svc.cluster.local 9090
   ```

4. strace the FUSE daemon:
   ```bash
   strace -p $(pgrep monofs-client) -f -e trace=file
   ```

## Encryption key

`MONOFS_ENCRYPTION_KEY` (64 hex chars) is in `../monofs/.env`.
Never rotate after ingesting data — existing blob archives become unreadable.

## Release checklist

1. Build + test in monofs repo
2. Run `uv run st-image build --partition dev-workspace`
3. Verify the new image with `docker run --rm -it dev-workspace-opencode:latest monofs-client --version`
4. Run `uv run st-release -p dev-workspace --bump --wait`
