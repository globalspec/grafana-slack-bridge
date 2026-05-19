# Kubernetes install

The bridge runs as a single-replica `Deployment` co-located with Grafana, consuming the Slack bot token from a `Secret`. Two example deployment patterns ship with the repo:

| Pattern | When to use |
|---------|-------------|
| [examples/kubernetes/simple/](../examples/kubernetes/simple/) | Smaller clusters, no Vault. Secret is created manually or via Sealed Secrets / External Secrets. |
| [examples/kubernetes/vault-csi/](../examples/kubernetes/vault-csi/) | HashiCorp Vault + Secrets Store CSI driver. Pulls tokens from Vault at pod startup; nothing sensitive in git. |

This guide walks through the simple pattern. For Vault: read [examples/kubernetes/vault-csi/README.md](../examples/kubernetes/vault-csi/README.md) after this page.

## Prerequisites

- A Kubernetes cluster running Grafana (any flavor: EKS, GKE, AKS, k3s, etc.)
- `kubectl` access
- A Slack bot token — see [docs/configuration.md](configuration.md#required-slack-bot-scopes)

## Install

### 1. Create the Slack token secret

The bridge expects an env var named `SLACK_BOT_TOKEN`. Put it in a Secret that the bridge will consume via `envFrom`:

```bash
kubectl -n grafana create secret generic grafana-slack-bot-token \
  --from-literal=SLACK_BOT_TOKEN=xoxb-…
```

If you already use this same secret for Grafana's built-in Slack notifier, reuse it — the bridge is happy to share.

### 2. Apply the bridge manifests

```bash
kubectl apply -f examples/kubernetes/simple/
```

This creates:

- `Deployment/grafana-slack-bridge` — 1 replica, non-root, read-only root FS
- `Service/grafana-slack-bridge` — `ClusterIP` on port 80 → pod port 8080

### 3. Verify

```bash
kubectl -n grafana get pods -l app.kubernetes.io/name=grafana-slack-bridge
kubectl -n grafana logs -l app.kubernetes.io/name=grafana-slack-bridge
kubectl -n grafana exec deploy/grafana-slack-bridge -- \
  python3 -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8080/healthz').read().decode())"
```

### 4. Point Grafana at it

Update the Grafana contact point URL to:

```
http://grafana-slack-bridge.grafana.svc/webhook?channel=<channel-id>
```

See [docs/configuration.md](configuration.md#grafana-contact-point-configuration) for full Grafana provisioning examples.

## Optional: in-bridge image rendering

If you want the bridge to render panel screenshots itself (recommended — see [README.md](../README.md)):

1. Create a Grafana service account with Viewer role, generate a token
2. Store it in a separate Secret:
   ```bash
   kubectl -n grafana create secret generic grafana-bridge-render-token \
     --from-literal=GRAFANA_TOKEN=glsa_…
   ```
3. The example deployment already references this Secret with `optional: true` — once it exists, the next pod restart picks it up

## Optional: NetworkPolicy

Restrict who can reach `/webhook`:

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: grafana-slack-bridge
  namespace: grafana
spec:
  podSelector:
    matchLabels:
      app.kubernetes.io/name: grafana-slack-bridge
  policyTypes: [Ingress]
  ingress:
    - from:
        - podSelector:
            matchLabels:
              app.kubernetes.io/name: grafana
      ports:
        - port: 8080
          protocol: TCP
```

This blocks all incoming traffic except from Grafana pods in the same namespace.

## ArgoCD

If you use GitOps with ArgoCD, point an `Application` at the manifests directory:

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: grafana-slack-bridge
  namespace: argocd
spec:
  project: default
  source:
    repoURL: https://github.com/your-org/your-deploy-repo.git
    targetRevision: main
    path: kubernetes/grafana-slack-bridge
  destination:
    server: https://kubernetes.default.svc
    namespace: grafana
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
```

Copy the example manifests into your deploy repo first, customize image tags / channel IDs there, and let ArgoCD pick up changes.

## Scaling

Don't. The bridge uses in-memory state — running multiple replicas would race for the same `groupKey`. The single replica handles thousands of alerts per minute fine.

If you genuinely need horizontal scaling, the change is small: replace the `state` dict in `bridge.py` with a Redis client. PRs welcome.

## Upgrading

Update the image tag in your manifests and apply:

```bash
# Edit examples/kubernetes/simple/deployment.yaml: image: …:v0.X.Y
kubectl apply -f examples/kubernetes/simple/
kubectl -n grafana rollout status deploy/grafana-slack-bridge
```

`strategy: Recreate` (not RollingUpdate) is the default — parallel pods would race for state. A restart loses tracked alert groups; the next state change for any in-flight alert posts a new message.

## Uninstalling

```bash
kubectl delete -f examples/kubernetes/simple/
kubectl -n grafana delete secret grafana-slack-bot-token grafana-bridge-render-token
```
