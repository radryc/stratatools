#!/bin/bash
set -euo pipefail

: "${ROUTER_ADDR:=monofs-external.storage-k8s.svc.cluster.local:9090}"
: "${MONOFS_MOUNT:=/mnt/monofs}"
: "${MONOFS_CACHE:=/var/cache/monofs}"
: "${WORKSPACE_USER:=developer}"
: "${WORKSPACE_GROUP:=$WORKSPACE_USER}"
if ! workspace_entry="$(getent passwd "$WORKSPACE_USER")"; then
  echo "[workspace] Linux user ${WORKSPACE_USER} does not exist" >&2
  exit 1
fi
IFS=: read -r _ _ _ _ _ workspace_home _ <<< "$workspace_entry"
if [[ -z "$workspace_home" ]]; then
  echo "[workspace] Linux user ${WORKSPACE_USER} does not have a home directory" >&2
  exit 1
fi
: "${WORKSPACE_HOME:=$workspace_home}"
: "${MONOFS_OVERLAY:=${WORKSPACE_HOME}/.monofs/overlay}"
: "${WORKSPACE_ROOT:=/workspace}"
: "${MONOFS_WORKSPACE_LINK:=${WORKSPACE_ROOT}/monofs}"
: "${OPENCODE_HOST:=0.0.0.0}"
: "${OPENCODE_PORT:=8888}"
: "${OPENCODE_SERVER_PASSWORD:=}"
: "${OPENCODE_SERVER_USERNAME:=opencode}"
: "${SSH_PORT:=22}"
: "${SSH_AUTHORIZED_KEYS_PATH:=/etc/dev-workspace/ssh/authorized_keys}"
: "${KUBECONFIG:=${WORKSPACE_HOME}/.kube/config}"
: "${KUBE_NAMESPACE:=}"
: "${MONOFS_CLIENT_LOG:=/var/log/monofs-client.log}"
: "${MONOFS_CLIENT_JSON_LOG:=/var/log/monofs-client.json}"

service_account_dir="/var/run/secrets/kubernetes.io/serviceaccount"

ensure_workspace_layout() {
  mkdir -p "$WORKSPACE_ROOT" "$MONOFS_MOUNT" "$MONOFS_CACHE" "$MONOFS_OVERLAY" "$(dirname "$KUBECONFIG")" "${WORKSPACE_HOME}/.config/opencode" "${WORKSPACE_HOME}/.ssh"
  touch "$MONOFS_CLIENT_LOG" "$MONOFS_CLIENT_JSON_LOG"
  chown -R "$WORKSPACE_USER:$WORKSPACE_GROUP" "$WORKSPACE_ROOT" "$MONOFS_MOUNT" "$MONOFS_CACHE" "$WORKSPACE_HOME/.monofs" "$WORKSPACE_HOME/.config" "$WORKSPACE_HOME/.ssh" "$(dirname "$KUBECONFIG")" "${WORKSPACE_HOME}/.config/opencode"
  chown "$WORKSPACE_USER:$WORKSPACE_GROUP" "$MONOFS_CLIENT_LOG" "$MONOFS_CLIENT_JSON_LOG"
  chmod 700 "$WORKSPACE_HOME/.ssh"

  if [[ ! -e "$MONOFS_WORKSPACE_LINK" ]]; then
    ln -s "$MONOFS_MOUNT" "$MONOFS_WORKSPACE_LINK"
  elif [[ -L "$MONOFS_WORKSPACE_LINK" ]]; then
    ln -sfn "$MONOFS_MOUNT" "$MONOFS_WORKSPACE_LINK"
  fi

  if [[ ! -e "$WORKSPACE_ROOT/README.monofs.txt" ]]; then
    cat >"$WORKSPACE_ROOT/README.monofs.txt" <<'EOF'
MonoFS OpenCode workspace

- MonoFS mount is exposed at ./monofs
- Remote SSH login is available as developer when the partition's ssh-authorized-keys config is populated
- Start a write session: mfs start
- Show pending changes: mfs status
- Commit changes: mfs commit
- kubectl uses an in-cluster kubeconfig generated on container start
- OpenCode web UI is available at the exposed HTTP port
EOF
    chown "$WORKSPACE_USER:$WORKSPACE_GROUP" "$WORKSPACE_ROOT/README.monofs.txt"
  fi
}

write_kubeconfig() {
  if [[ ! -f "$service_account_dir/token" || -z "${KUBERNETES_SERVICE_HOST:-}" ]]; then
    echo "[workspace] service account credentials not present; kubectl will need a manual kubeconfig" >&2
    return 0
  fi

  local namespace server token
  namespace="${KUBE_NAMESPACE:-$(cat "$service_account_dir/namespace")}"
  server="https://${KUBERNETES_SERVICE_HOST}:${KUBERNETES_SERVICE_PORT_HTTPS:-443}"
  token="$(cat "$service_account_dir/token")"

  cat >"$KUBECONFIG" <<EOF
apiVersion: v1
kind: Config
clusters:
  - cluster:
      certificate-authority: ${service_account_dir}/ca.crt
      server: ${server}
    name: in-cluster
contexts:
  - context:
      cluster: in-cluster
      namespace: ${namespace}
      user: service-account
    name: in-cluster
current-context: in-cluster
users:
  - name: service-account
    user:
      token: ${token}
EOF

  chmod 600 "$KUBECONFIG"
  chown "$WORKSPACE_USER:$WORKSPACE_GROUP" "$KUBECONFIG"
}

configure_ssh_access() {
  install -d -o "$WORKSPACE_USER" -g "$WORKSPACE_GROUP" -m 0700 "$WORKSPACE_HOME/.ssh"
  rm -f "$WORKSPACE_HOME/.ssh/authorized_keys"

  if [[ -f "$SSH_AUTHORIZED_KEYS_PATH" ]]; then
    grep -Ev '^[[:space:]]*(#|$)' "$SSH_AUTHORIZED_KEYS_PATH" > "$WORKSPACE_HOME/.ssh/authorized_keys" || true
    if [[ -s "$WORKSPACE_HOME/.ssh/authorized_keys" ]]; then
      chown "$WORKSPACE_USER:$WORKSPACE_GROUP" "$WORKSPACE_HOME/.ssh/authorized_keys"
      chmod 600 "$WORKSPACE_HOME/.ssh/authorized_keys"
      echo "[workspace] installed SSH authorized_keys for ${WORKSPACE_USER}" >&2
      return 0
    fi
    rm -f "$WORKSPACE_HOME/.ssh/authorized_keys"
  fi

  echo "[workspace] no SSH public keys configured; Remote SSH login will remain disabled" >&2
}

write_opencode_config() {
  local cfg="${WORKSPACE_HOME}/.config/opencode/config.json"
  if [[ -f "$cfg" ]]; then
    return 0
  fi

  cat >"$cfg" <<'JSON'
{
  "model": "deepseek/deepseek-chat",
  "permission": {
    "skill": {
      "*": "allow"
    }
  },
  "server": {
    "hostname": "0.0.0.0",
    "port": 8888
  }
}
JSON
  chown "$WORKSPACE_USER:$WORKSPACE_GROUP" "$cfg"
  echo "[workspace] wrote default OpenCode config to $cfg" >&2
}

install_skills() {
  local skills_dst="${WORKSPACE_HOME}/.agents/skills"

  mkdir -p "$skills_dst"
  chown "$WORKSPACE_USER:$WORKSPACE_GROUP" "$skills_dst" "$(dirname "$skills_dst")"

  # Skip if already installed (detected by marker)
  if [[ -f "$skills_dst/.installed" ]]; then
    return 0
  fi

  echo "[workspace] installing agent skills" >&2

  # strata-partition — creating/maintaining partitions, intents, assets
  mkdir -p "$skills_dst/strata-partition"
  cat >"$skills_dst/strata-partition/SKILL.md" <<'SKILL'
---
name: strata-partition
description: >
  Create, modify, and maintain Strata partitions — Guardian desired-state YAML
  documents that define Kubernetes workloads via intents and assets (Compute,
  Volume, Config, ImageBuild). Use when creating a new partition, adding an
  intent or asset, releasing a partition, stamping image refs, or debugging
  partition reconciliation.
---

# Strata Partition Workflow

## Creating a new partition

1. Copy from `partitions/_template/`
2. Edit `config.yaml` (Kind: Partition)
3. Add to `PARTITIONS_LIST` in `src/stratatools/image/__init__.py`
4. Add image recipes to `BUILD_RECIPES` or `IMAGE_TAR_RECIPES` if needed

## Asset types

- **Compute** — K8s deployment, references a payload `.k8s.yaml` snippet
- **Volume** — PVC spec (size, accessMode)
- **Config** — text data mounted into pods (e.g. SSH keys)
- **ImageBuild** — OCI tar or source-based build

## Building & Releasing

```bash
uv run st-image build --partition <name>
uv run st-image push --partition <name>
uv run st-image stamp --partition <name>
uv run st-release -p <name> --bump --wait
```

## Edge exposure

Add to K8s payload: `serviceAnnotations: { guardian.intent/expose: 'true' }`
SKILL
  chown "$WORKSPACE_USER:$WORKSPACE_GROUP" "$skills_dst/strata-partition/SKILL.md"

  # strata-monofs — MonoFS development flow
  mkdir -p "$skills_dst/strata-monofs"
  cat >"$skills_dst/strata-monofs/SKILL.md" <<'SKILL'
---
name: strata-monofs
description: >
  MonoFS development flow — build, test, debug, and release the MonoFS
  FUSE-backed virtual filesystem. Use when working in the monofs sibling
  repo, debugging FUSE mount issues, building images, or changing the
  MonoFS protocol.
---

# MonoFS Development Flow

## Building

```bash
cd ../monofs
bazel build //cmd/...
bazel test //...
```

## Docker image build (via stratatools)

```bash
uv run st-image build --partition dev-workspace
docker build -t monofs-client:dev-base --target client ../monofs
```

## Running monofs-client

```bash
monofs-client --router=<addr> --mount=/mnt/monofs --virtual-monorepo --writable
```

## Write sessions (mfs)

```bash
mfs setup --mount /mnt/monofs
mfs start; mfs status; mfs commit
```

## Debugging FUSE

```bash
mountpoint -q /mnt/monofs
tail -f /var/log/monofs-client.json
strace -p $(pgrep monofs-client) -f -e trace=file
```
SKILL
  chown "$WORKSPACE_USER:$WORKSPACE_GROUP" "$skills_dst/strata-monofs/SKILL.md"

  # strata-bootstrap — bootstrap/deployment workflows
  mkdir -p "$skills_dst/strata-bootstrap"
  cat >"$skills_dst/strata-bootstrap/SKILL.md" <<'SKILL'
---
name: strata-bootstrap
description: >
  Bootstrap and manage the Strata development cluster. Use when setting up
  the dev environment, debugging bootstrap failures, managing cluster state,
  or configuring storage/guardian phases.
---

# Strata Bootstrap

```bash
uv run st-setup                  # Clone repos + create kind cluster
uv run st-bootstrap build        # Build Go CLIs + Docker images
uv run st-bootstrap deploy       # Full deploy (build → load → deploy → stamp)
uv run st-bootstrap rollout      # Rebuild images + restart deployments
uv run st-bootstrap stop         # Scale to zero
uv run st-bootstrap destroy      # Delete namespaces
```

## Env config

`bootstrap.local.env` (gitignored). Key vars:
- `LB_USER_SERVICE_PORTS` — forwarded ports (default: 9191 8888)
- `GUARDIAN_REPO_DIR`, `MONOFS_REPO_DIR` — sibling repo paths
- `MONOFS_ENCRYPTION_KEY` — 64-char hex from `../monofs/.env`

## Debugging

- ImagePullBackOff → run `st-image push`
- CrashLoopBackOff → check logs, FUSE device, permissions
- Pending → check PVC binding, node resources
SKILL
  chown "$WORKSPACE_USER:$WORKSPACE_GROUP" "$skills_dst/strata-bootstrap/SKILL.md"

  # go-development — Go patterns
  mkdir -p "$skills_dst/go-development"
  cat >"$skills_dst/go-development/SKILL.md" <<'SKILL'
---
name: go-development
description: >
  Go development patterns — module management, Bazel builds, testing,
  error wrapping, context propagation, gRPC. Use when writing or modifying
  Go code, fixing build errors, writing Go tests, or working with protobuf.
---

# Go Development

## Build (Bazel)

```bash
bazel build //cmd/...
bazel test //...
```

## Testing

```bash
go test ./...
go test -v -run TestFoo ./pkg/
go test -race ./...
```

## Patterns

- Wrap errors: `fmt.Errorf("op failed: %w", err)`
- Context: `ctx, cancel := context.WithTimeout(...)`
- Table-driven tests preferred
SKILL
  chown "$WORKSPACE_USER:$WORKSPACE_GROUP" "$skills_dst/go-development/SKILL.md"

  # python-typer — Python/Typer patterns
  mkdir -p "$skills_dst/python-typer"
  cat >"$skills_dst/python-typer/SKILL.md" <<'SKILL'
---
name: python-typer
description: >
  Python development with uv, Typer CLI, and unittest. Use when modifying
  Python CLI code, adding commands, writing tests, or debugging shell-out
  patterns in Typer apps.
---

# Python / Typer (stratatools)

## Module structure

```
src/stratatools/<name>/__init__.py  # exports Typer app
```

## CLI patterns

```python
from stratatools.util import info, run, warn, die
run(["kubectl", "apply", "-f", m], dry_run=dry_run)
```

## Testing

```bash
python -m unittest tests.test_foo
python -m unittest discover -s tests
```
SKILL
  chown "$WORKSPACE_USER:$WORKSPACE_GROUP" "$skills_dst/python-typer/SKILL.md"

  # kubernetes-debug — K8s debugging
  mkdir -p "$skills_dst/kubernetes-debug"
  cat >"$skills_dst/kubernetes-debug/SKILL.md" <<'SKILL'
---
name: kubernetes-debug
description: >
  Debug Kubernetes deployments, pods, services, and networking in kind
  clusters. Use when pods are failing, services are unreachable, RBAC
  denies access, or resources are not applying correctly.
---

# Kubernetes Debugging

```bash
kubectl get pods -A
kubectl describe pod <name> -n <ns>
kubectl logs <pod> -n <ns> --previous
kubectl get events -A --sort-by='.lastTimestamp'
```

## Common fixes

- ImagePullBackOff → `kind load docker-image <img> --name strata`
- CrashLoopBackOff → check FUSE device, privileged mode, env vars
- Pending → check PVC, `kubectl get pvc -A`
- RBAC denied → `kubectl auth can-i --list -n <ns>`
SKILL
  chown "$WORKSPACE_USER:$WORKSPACE_GROUP" "$skills_dst/kubernetes-debug/SKILL.md"

  touch "$skills_dst/.installed"
  chown "$WORKSPACE_USER:$WORKSPACE_GROUP" "$skills_dst/.installed"
}

start_monofs_client() {
  echo "[workspace] starting monofs-client against ${ROUTER_ADDR}" >&2
  su - "$WORKSPACE_USER" -c "/usr/local/bin/monofs-client \
    --router=${ROUTER_ADDR} \
    --mount=${MONOFS_MOUNT} \
    --cache=${MONOFS_CACHE} \
    --virtual-monorepo \
    --writable \
    --overlay=${MONOFS_OVERLAY} \
    --debug \
    --log-file=${MONOFS_CLIENT_JSON_LOG}" \
    >"$MONOFS_CLIENT_LOG" 2>&1 &
}

wait_for_mount() {
  local attempt
  for attempt in $(seq 1 30); do
    if mountpoint -q "$MONOFS_MOUNT" 2>/dev/null; then
      echo "[workspace] monofs mounted at ${MONOFS_MOUNT}" >&2
      return 0
    fi
    sleep 1
  done

  echo "[workspace] monofs mount is not ready yet; check ${MONOFS_CLIENT_LOG}" >&2
  return 0
}

start_sshd() {
  mkdir -p /run/sshd
  rm -f /run/sshd.pid
  if ! compgen -G '/etc/ssh/ssh_host_*_key' >/dev/null; then
    ssh-keygen -A
  fi

  echo "[workspace] starting sshd on port ${SSH_PORT}" >&2
  /usr/sbin/sshd -o Port="$SSH_PORT"
}

start_opencode() {
  local cmd=(
    env
    MONOFS_BINARY_DIR=/usr/local/bin
    MONOFS_ROUTER="$ROUTER_ADDR"
    MONOFS_MOUNT="$MONOFS_MOUNT"
    MONOFS_OVERLAY="$MONOFS_OVERLAY"
    MONOFS_CACHE="$MONOFS_CACHE"
    MONOFS_WORKSPACE_ROOT="$WORKSPACE_ROOT"
    HOME="$WORKSPACE_HOME"
    USER="$WORKSPACE_USER"
  )

  if [[ -n "${OPENCODE_SERVER_PASSWORD:-}" ]]; then
    cmd+=("OPENCODE_SERVER_PASSWORD=${OPENCODE_SERVER_PASSWORD}")
    if [[ -n "${OPENCODE_SERVER_USERNAME:-}" ]]; then
      cmd+=("OPENCODE_SERVER_USERNAME=${OPENCODE_SERVER_USERNAME}")
    fi
  fi

  cmd+=(
    opencode
    web
    --hostname "$OPENCODE_HOST"
    --port "$OPENCODE_PORT"
  )

  exec su - "$WORKSPACE_USER" -c "$(printf '%q ' "${cmd[@]}")"
}

ensure_workspace_layout
write_kubeconfig
configure_ssh_access
write_opencode_config
install_skills
start_monofs_client
wait_for_mount
start_sshd
start_opencode
