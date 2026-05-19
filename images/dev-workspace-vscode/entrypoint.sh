#!/bin/bash
set -euo pipefail

: "${ROUTER_ADDR:=monofs-external.storage-k8s.svc.cluster.local:9090}"
: "${MONOFS_MOUNT:=/mnt/monofs}"
: "${MONOFS_CACHE:=/var/cache/monofs}"
: "${WORKSPACE_USER:=openvscode-server}"
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
: "${VSCODE_HOST:=0.0.0.0}"
: "${VSCODE_PORT:=3000}"
: "${VSCODE_CONNECTION_TOKEN:=dev-workspace-token-change-me}"
: "${VSCODE_DEFAULT_FOLDER:=${WORKSPACE_ROOT}}"
: "${VSCODE_EXTENSIONS_DIR:=${WORKSPACE_HOME}/.openvscode-server/extensions}"
: "${SSH_PORT:=22}"
: "${SSH_AUTHORIZED_KEYS_PATH:=/etc/dev-workspace/ssh/authorized_keys}"
: "${KUBECONFIG:=${WORKSPACE_HOME}/.kube/config}"
: "${KUBE_NAMESPACE:=}"
: "${MONOFS_CLIENT_LOG:=/var/log/monofs-client.log}"
: "${MONOFS_CLIENT_JSON_LOG:=/var/log/monofs-client.json}"

service_account_dir="/var/run/secrets/kubernetes.io/serviceaccount"

ensure_workspace_layout() {
  mkdir -p "$WORKSPACE_ROOT" "$MONOFS_MOUNT" "$MONOFS_CACHE" "$MONOFS_OVERLAY" "$(dirname "$KUBECONFIG")" "$VSCODE_EXTENSIONS_DIR" "$WORKSPACE_HOME/.ssh"
  touch "$MONOFS_CLIENT_LOG" "$MONOFS_CLIENT_JSON_LOG"
  chown -R "$WORKSPACE_USER:$WORKSPACE_GROUP" "$WORKSPACE_ROOT" "$MONOFS_MOUNT" "$MONOFS_CACHE" "$WORKSPACE_HOME/.monofs" "$WORKSPACE_HOME/.ssh" "$(dirname "$KUBECONFIG")" "$VSCODE_EXTENSIONS_DIR"
  chown "$WORKSPACE_USER:$WORKSPACE_GROUP" "$MONOFS_CLIENT_LOG" "$MONOFS_CLIENT_JSON_LOG"
  chmod 700 "$WORKSPACE_HOME/.ssh"

  if [[ ! -e "$WORKSPACE_ROOT/monofs" ]]; then
    ln -s "$MONOFS_MOUNT" "$WORKSPACE_ROOT/monofs"
  elif [[ -L "$WORKSPACE_ROOT/monofs" ]]; then
    ln -sfn "$MONOFS_MOUNT" "$WORKSPACE_ROOT/monofs"
  fi

  if [[ ! -e "$WORKSPACE_ROOT/README.monofs.txt" ]]; then
    cat >"$WORKSPACE_ROOT/README.monofs.txt" <<'EOF'
MonoFS VS Code workspace

- MonoFS mount is exposed at ./monofs
- Remote SSH login is available as openvscode-server when the partition's ssh-authorized-keys config is populated
- Start a write session: mfs start
- Show pending changes: mfs status
- Commit changes: mfs commit
- kubectl uses an in-cluster kubeconfig generated on container start
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
  exec su - "$WORKSPACE_USER" -c "$(printf '%q ' "${cmd[@]}")"
}

ensure_workspace_layout
write_kubeconfig
configure_ssh_access
start_monofs_client
wait_for_mount
start_sshd
start_vscode