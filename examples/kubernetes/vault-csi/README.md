# Kubernetes install with HashiCorp Vault (Secrets Store CSI)

This pattern pulls the Grafana service-account render token from Vault at pod startup via the Secrets Store CSI driver, instead of keeping a Secret in git. Useful when:

- You already run Vault and want one place for all secrets
- Your compliance posture requires that nothing sensitive ever lives in cluster etcd unencrypted
- You want short-TTL Vault leases and automatic rotation

It does **not** cover the Slack bot token — that's still expected as a regular k8s Secret because Vault adds latency to startup that isn't worth it for one rarely-rotated value. If you do want to also pull `SLACK_BOT_TOKEN` from Vault, extend the SecretProviderClass below.

## Prerequisites

- HashiCorp Vault reachable from the cluster
- Vault's Kubernetes auth method enabled and bound to your cluster's service-account issuer
- [Secrets Store CSI Driver](https://secrets-store-csi-driver.sigs.k8s.io/) installed
- [Vault provider for CSI](https://developer.hashicorp.com/vault/docs/platform/k8s/csi) installed
- A Grafana service-account token to store in Vault (for in-bridge image rendering)

## Vault setup

The included `vault-setup.sh` walks through the one-time Vault configuration:

1. Writes a Vault policy `grafana-slack-bridge` granting read on the bridge's KV path
2. Writes a Vault role `grafana-slack-bridge` bound to the cluster's k8s ServiceAccount
3. Seeds the Grafana token at `<KV mount>/grafana-slack-bridge/grafana-render-token`

Inspect and customize for your Vault paths before running:

```bash
$EDITOR vault-setup.sh
./vault-setup.sh
```

## Kubernetes manifests

Apply in order (or all at once with `kubectl apply -f .`):

1. **`serviceaccount.yaml`** — dedicated SA for Vault to bind against
2. **`secret-provider-class.yaml`** — declares which Vault path maps to which k8s Secret
3. **`deployment.yaml`** — runs the bridge as that SA, mounts the CSI volume

```bash
# Slack bot token still as a regular Secret
kubectl -n grafana create secret generic grafana-slack-bot-token \
  --from-literal=SLACK_BOT_TOKEN=xoxb-...

# Then apply the bridge
kubectl apply -f .
```

## How the wiring works

1. Pod starts with `serviceAccountName: grafana-slack-bridge`
2. Secrets Store CSI driver mounts the `SecretProviderClass` as a volume
3. Driver authenticates to Vault using the pod's k8s JWT against the bridge's Vault role
4. Driver reads the secret from Vault and:
   - Materializes it as a file at `/mnt/vault-secrets/grafana-render-token`, AND
   - Creates / updates a regular k8s Secret `grafana-bridge-render-token` (via `secretObjects`)
5. The deployment consumes the k8s Secret via `envFrom`, so the application code doesn't have to read the CSI file
6. The CSI mount is what keeps the driver actively syncing — without a pod referencing the SPC, it's inert

## Placeholders to replace

Search-and-replace these in the example files before applying:

| Placeholder | Replace with | Example |
|-------------|--------------|---------|
| `<VAULT_ADDR>` | Your Vault address | `https://vault.example.com` |
| `<VAULT_AUTH_PATH>` | The k8s auth mount path | `kubernetes-prod` |
| `<VAULT_KV_PATH>` | KV v2 logical path to the token (no `data/` prefix in the policy, but yes in the SPC) | `infrastructure/k8s/grafana-slack-bridge/grafana-render-token` |
| `<VAULT_KV_MOUNT>` | KV v2 mount name | `secret` (default) or `it/kv` (custom) |

## Troubleshooting

```bash
# Did the CSI driver materialize the synced k8s Secret?
kubectl -n grafana get secret grafana-bridge-render-token -o yaml

# Pod stuck mounting the CSI volume?
kubectl -n grafana describe pod -l app.kubernetes.io/name=grafana-slack-bridge

# Vault provider logs
kubectl -n vault logs -l app=vault-csi-provider
```

Common failure modes:

- **Vault role bound to wrong namespace/SA name** → fix `bound_service_account_*` in the Vault role
- **Pod's k8s JWT not accepted** → check `kubernetes_host` in the auth method config; for EKS use the OIDC issuer URL
- **KV v2 read path missing `data/`** → the Vault policy must include `data/` in the path; KV v1 doesn't
