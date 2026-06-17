---
name: strata-bootstrap
description: >
  Bootstrap and manage the Strata development cluster — kind cluster setup,
  image builds, K8s deployments, port-forwarding, and environment configuration.
  Use when setting up the dev environment, debugging bootstrap failures,
  managing cluster state, or configuring storage/guardian phases.
---

# Strata Bootstrap Workflow

## Quick commands

```bash
uv run st-setup                  # Clone sibling repos, create kind cluster
uv run st-bootstrap build        # Build Go CLIs + Docker images
uv run st-bootstrap deploy       # Full bootstrap (build → load → deploy → stamp)
uv run st-bootstrap rollout      # Rebuild images + restart deployments
uv run st-bootstrap stamp-urls   # Refresh endpoint stamps in partition configs
uv run st-bootstrap stop         # Scale deployments to zero
uv run st-bootstrap destroy      # Delete namespaces
uv run st-bootstrap watch-pf     # Keep port-forward alive
```

## Environment configuration

`bootstrap.local.env` is a shell-style env file (gitignored).

Critical env vars:
- `LB_USER_SERVICE_PORTS` — ports to forward (default: `9191 8888`)
- `GUARDIAN_REPO_DIR`, `MONOFS_REPO_DIR` — override sibling repo paths
- `MONOFS_ENCRYPTION_KEY` — 64-char hex, read from `../monofs/.env`

## Env loading order (critical)

1. `_load_bootstrap_env()` reads env file into `os.environ`
2. `_reload_bootstrap_modules()` reloads `storage` and `guardian` modules
   to re-evaluate module-level `os.environ.get()` calls

Module-level defaults in `storage.py` and `guardian.py` are evaluated at
import time. The reload mechanism ensures they pick up env file changes.

## Bootstrap phases

### storage.py (Phase 1)
- Creates kind cluster
- Deploys MonoFS infrastructure
- Sets up storage namespaces, PVCs, services

### guardian.py (Phase 2)
- Deploys Guardian controller
- Configures cluster RBAC
- Sets up load balancer edge

### cli.py
- Orchestrates phases
- Applies cluster-admin RBAC for dev-workspace
- Manages port-forward

## Debugging bootstrap failures

1. Check cluster status:
   ```bash
   kubectl get nodes
   kubectl get pods -A
   ```

2. Check Guardian reconciliation:
   ```bash
   kubectl logs -n guardian-k8s deployment/guardian
   ```

3. Check image presence:
   ```bash
   docker images | grep sha256
   kubectl describe pod <name> -n <ns>
   ```

4. Common issues:
   - ImagePullBackOff → images not loaded into kind, run `st-image push`
   - CrashLoopBackOff → check logs, often FUSE or mount permissions
   - Pending pods → check PVC binding, node resources

## Port forwarding

`st-bootstrap watch-pf` runs background port-forward for:
- Load balancer external service (`LB_USER_SERVICE_PORTS`)

Manual port-forward:
```bash
kubectl port-forward -n lb-edge svc/monofs-external 8888:8888 &
```

## Resetting

```bash
# Stop all workloads
uv run st-bootstrap stop

# Delete namespaces (keeps cluster)
uv run st-bootstrap destroy

# Full reset: delete kind cluster + recreate
kind delete cluster --name strata
uv run st-setup
```
