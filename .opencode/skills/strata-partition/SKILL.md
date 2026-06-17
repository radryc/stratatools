---
name: strata-partition
description: >
  Create, modify, and maintain Strata partitions — Guardain desired-state YAML
  documents that define Kubernetes workloads via intents and assets (Compute,
  Volume, Config, ImageBuild). Use when creating a new partition, adding an
  intent or asset, releasing a partition, stamping image refs, or debugging
  partition reconciliation.
---

# Strata Partition Workflow

## Quick reference

```
partitions/<name>/
  config.yaml            # Kind: Partition — registers with Guardian
  intents/               # Kind: Intent  — desired-state YAML
    <name>.yaml
  payloads/              # K8s manifests + OCI tar images
    <asset>.k8s.yaml
    images/
    sources/
```

## Creating a new partition

1. Copy from `partitions/_template/`:
   ```bash
   cp -r partitions/_template partitions/<new-name>
   ```

2. Edit `partitions/<new-name>/config.yaml`:
   ```yaml
   apiVersion: guardian/v1alpha1
   kind: Partition
   metadata:
     name: <new-name>
     labels:
       role: <development|infrastructure|application>
   spec:
     deletionPolicy: orphan
     reconciliation:
       mode: auto
       interval: 1m
     labels:
       stack: k8s
       component: <new-name>
       topology: kubernetes
       managedBy: guardian
       endpoint: http://...
   ```

3. Add to `PARTITIONS_LIST` in `src/stratatools/image/__init__.py` (line 37-40).

4. If the partition needs images, add to `BUILD_RECIPES` (line 79) or `IMAGE_TAR_RECIPES` (line 98).

## Adding an Intent

Create `partitions/<name>/intents/<intent-name>.yaml`:

```yaml
apiVersion: guardian/v1alpha1
kind: Intent
metadata:
  name: <intent-name>
spec:
  intentType: standard
  targetPusher: k8s-main
  target:
    cluster: k8s-main
    namespace: <namespace>
  locked: false
  assets:
    - type: Compute
      name: <asset-name>
      ...
```

If the intent depends on another intent's ImageBuild output, add `joins: [<other-intent>]`.

## Asset types

### Compute
```yaml
- type: Compute
  name: my-app
  dependsOn: [volume-name]
  payload:
    k8s: /partitions/<name>/payloads/<asset>.k8s.yaml
  properties:
    image: <image-ref>
    env:
      KEY: value
    ports:
      - containerPort: 8080
        name: http
        servicePort: 80
    volumeMounts:
      - path: /data
        volume: volume-name
```

### Volume
```yaml
- type: Volume
  name: data-volume
  properties:
    accessMode: ReadWriteOnce
    size: 10Gi
```

### Config
```yaml
- type: Config
  name: my-config
  properties:
    data:
      key: value
    format: text
```

### ImageBuild (tar mode — pre-built OCI tar)
```yaml
- type: ImageBuild
  name: images
  properties:
    mode: tar
    images:
      - image: my-image:latest
        tarPath: /partitions/<name>/payloads/images/my-image.tar
```

## Building & Releasing

```bash
# Build images for one partition
uv run st-image build --partition <name>

# Push/cluster-load + stamp immutable refs
uv run st-image push --partition <name>
uv run st-image stamp --partition <name>

# Full cycle: build → push → stamp
uv run st-image all --partition <name>

# Release to Guardian (pushes partition + applies)
uv run st-release -p <name> --bump --wait
```

## Stamping

Image refs in intent YAML must be immutable (`sha256-*` prefix).
Use `st-image stamp` which inspects local Docker images and replaces
`:latest` tags with content-addressed refs.

## Edge / Load Balancer exposure

Add these annotations to the K8s payload:
```yaml
serviceAnnotations:
  guardian.intent/expose: 'true'
  guardian.intent/service-name: my-edge
  guardian.intent/external-port: '8888'
```

This triggers the `lb-k8s-agent` to register the service in the edge load balancer.
