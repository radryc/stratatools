#!/bin/bash
set -euo pipefail

: "${ROUTER_ADDR:=monofs-external.storage-k8s.svc.cluster.local:9090}"
: "${MONOFS_MOUNT:=/mnt/monofs}"
: "${MONOFS_CACHE:=/var/cache/monofs}"
: "${MONOFS_OVERLAY:=/home/monofs/.monofs/overlay}"
: "${WORKSPACE_ROOT:=/workspace}"
: "${VSCODE_HOST:=0.0.0.0}"
: "${VSCODE_PORT:=3000}"
: "${VSCODE_CONNECTION_TOKEN:=dev-workspace-token-change-me}"
: "${VSCODE_DEFAULT_FOLDER:=${WORKSPACE_ROOT}}"
: "${VSCODE_EXTENSIONS_DIR:=/home/monofs/.openvscode-server/extensions}"
: "${KUBECONFIG:=/home/monofs/.kube/config}"
: "${KUBE_NAMESPACE:=}"
: "${MONOFS_CLIENT_LOG:=/var/log/monofs-client.log}"
: "${MONOFS_CLIENT_JSON_LOG:=/var/log/monofs-client.json}"

service_account_dir="/var/run/secrets/kubernetes.io/serviceaccount"

ensure_workspace_layout() {
  mkdir -p "$WORKSPACE_ROOT" "$MONOFS_MOUNT" "$MONOFS_CACHE" "$MONOFS_OVERLAY" "$(dirname "$KUBECONFIG")" "$VSCODE_EXTENSIONS_DIR"
  touch "$MONOFS_CLIENT_LOG" "$MONOFS_CLIENT_JSON_LOG"
  chown -R monofs:monofs "$WORKSPACE_ROOT" "$MONOFS_CACHE" /home/monofs/.monofs "$(dirname "$KUBECONFIG")" "$VSCODE_EXTENSIONS_DIR"
  chown monofs:monofs "$MONOFS_CLIENT_LOG" "$MONOFS_CLIENT_JSON_LOG"

  if [[ ! -e "$WORKSPACE_ROOT/monofs" ]]; then
    ln -s "$MONOFS_MOUNT" "$WORKSPACE_ROOT/monofs"
  elif [[ -L "$WORKSPACE_ROOT/monofs" ]]; then
    ln -sfn "$MONOFS_MOUNT" "$WORKSPACE_ROOT/monofs"
  fi

  if [[ ! -e "$WORKSPACE_ROOT/README.monofs.txt" ]]; then
    cat >"$WORKSPACE_ROOT/README.monofs.txt" <<'EOF'
MonoFS VS Code workspace

- MonoFS mount is exposed at ./monofs
- Start a write session: monofs-session start
- Show pending changes: monofs-session status
- Commit changes: monofs-session commit
- kubectl uses an in-cluster kubeconfig generated on container start
EOF
    chown monofs:monofs "$WORKSPACE_ROOT/README.monofs.txt"
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
  chown monofs:monofs "$KUBECONFIG"
}

start_monofs_client() {
  echo "[workspace] starting monofs-client against ${ROUTER_ADDR}" >&2
  su - monofs -c "/usr/local/bin/monofs-client \
    --router=${ROUTER_ADDR} \
    --mount=${MONOFS_MOUNT} \
    --cache=${MONOFS_CACHE} \
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

start_vscode() {
  local cmd=(
    env
    MONOFS_VSCODE_PROFILE=devWorkspacePartition
    MONOFS_BINARY_DIR=/usr/local/bin
    MONOFS_ROUTER="$ROUTER_ADDR"
    MONOFS_MOUNT="$MONOFS_MOUNT"
    MONOFS_OVERLAY="$MONOFS_OVERLAY"
    MONOFS_CACHE="$MONOFS_CACHE"
    MONOFS_WORKSPACE_ROOT="$WORKSPACE_ROOT"
    openvscode-server
    --host "$VSCODE_HOST"
    --port "$VSCODE_PORT"
  )

  if [[ -n "$VSCODE_CONNECTION_TOKEN" ]]; then
    cmd+=(--connection-token "$VSCODE_CONNECTION_TOKEN")
  else
    cmd+=(--without-connection-token)
  fi

    cmd+=(--extensions-dir "$VSCODE_EXTENSIONS_DIR")
  cmd+=("$VSCODE_DEFAULT_FOLDER")
  exec su - monofs -c "$(printf '%q ' "${cmd[@]}")"
}

ensure_workspace_layout
write_kubeconfig
start_monofs_client
wait_for_mount
start_vscode