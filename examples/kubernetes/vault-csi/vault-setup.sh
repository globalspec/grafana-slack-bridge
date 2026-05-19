#!/bin/bash
#
# One-time Vault setup for grafana-slack-bridge: provisions a least-privilege
# Vault role bound to the bridge's k8s ServiceAccount, plus a policy granting
# read on the Grafana render token KV path.
#
# Adjust the values in the CONFIG section below before running.
#
# Prerequisites:
#   - vault CLI authenticated with sufficient admin permissions
#   - kubectl context pointing at the target cluster
#   - The bridge ServiceAccount already applied (serviceaccount.yaml)
#   - A Grafana service-account token to store (see Step 0 below)

set -euo pipefail

# ─── CONFIG ─────────────────────────────────────────────────────────────
# Replace these with values for your environment.
VAULT_ADDR="${VAULT_ADDR:-https://vault.example.com}"
VAULT_AUTH_PATH="${VAULT_AUTH_PATH:-kubernetes}"      # auth mount path; default is "kubernetes"
VAULT_KV_MOUNT="${VAULT_KV_MOUNT:-secret}"            # KV v2 mount name
VAULT_KV_PATH="${VAULT_KV_PATH:-infrastructure/k8s/grafana-slack-bridge/grafana-render-token}"
NAMESPACE="${NAMESPACE:-grafana}"
SERVICEACCOUNT="${SERVICEACCOUNT:-grafana-slack-bridge}"
ROLE_NAME="${ROLE_NAME:-grafana-slack-bridge}"
POLICY_NAME="${POLICY_NAME:-grafana-slack-bridge}"
TOKEN_TTL="${TOKEN_TTL:-1h}"

export VAULT_ADDR

echo "Configuration:"
echo "  VAULT_ADDR       = $VAULT_ADDR"
echo "  VAULT_AUTH_PATH  = $VAULT_AUTH_PATH"
echo "  VAULT_KV_MOUNT   = $VAULT_KV_MOUNT"
echo "  VAULT_KV_PATH    = $VAULT_KV_PATH"
echo "  NAMESPACE        = $NAMESPACE"
echo "  SERVICEACCOUNT   = $SERVICEACCOUNT"
echo "  ROLE_NAME        = $ROLE_NAME"
echo "  POLICY_NAME      = $POLICY_NAME"
echo
read -r -p "Proceed? [y/N] " yn
case "$yn" in y|Y) ;; *) exit 0 ;; esac
echo

# ─── Step 0: mint the Grafana service-account token ─────────────────────
# Done via the Grafana API. The exact command depends on your Grafana setup;
# below is the standard /api/serviceaccounts flow. Capture the returned
# `key` value — that's what you'll paste in Step 4.
#
#   GRAFANA_ADMIN="<your-admin-token>"
#   SA_ID=$(curl -sS -X POST -H "Authorization: Bearer $GRAFANA_ADMIN" \
#       -H "Content-Type: application/json" \
#       -d '{"name":"grafana-slack-bridge","role":"Viewer","isDisabled":false}' \
#       https://grafana.example.com/api/serviceaccounts | jq -r '.id')
#   curl -sS -X POST -H "Authorization: Bearer $GRAFANA_ADMIN" \
#       -H "Content-Type: application/json" \
#       -d "{\"name\":\"render-$(date +%Y-%m-%d)\"}" \
#       "https://grafana.example.com/api/serviceaccounts/$SA_ID/tokens"

# ─── Step 1: verify kubernetes auth is enabled ──────────────────────────
echo "[1/4] Verifying $VAULT_AUTH_PATH/ auth method exists..."
if ! vault auth list 2>/dev/null | grep -q "^${VAULT_AUTH_PATH}/"; then
    echo "    ERROR: $VAULT_AUTH_PATH/ auth not enabled in Vault."
    echo "    Enable it first: vault auth enable -path=$VAULT_AUTH_PATH kubernetes"
    exit 1
fi
echo "    OK"

# ─── Step 2: write the Vault policy ─────────────────────────────────────
echo "[2/4] Writing Vault policy '$POLICY_NAME'..."
vault policy write "$POLICY_NAME" - <<POLICY
# Read the Grafana render service-account token (KV v2 — data/ prefix required)
path "${VAULT_KV_MOUNT}/data/${VAULT_KV_PATH}" {
  capabilities = ["read"]
}
POLICY
echo "    OK"

# ─── Step 3: bind the policy to the k8s SA ──────────────────────────────
echo "[3/4] Writing Vault role '$ROLE_NAME'..."
vault write "auth/$VAULT_AUTH_PATH/role/$ROLE_NAME" \
    bound_service_account_names="$SERVICEACCOUNT" \
    bound_service_account_namespaces="$NAMESPACE" \
    policies="$POLICY_NAME" \
    ttl="$TOKEN_TTL"
echo "    OK"

# ─── Step 4: store the Grafana token ────────────────────────────────────
echo "[4/4] Storing Grafana render token in Vault..."
if [ -z "${GRAFANA_RENDER_TOKEN:-}" ]; then
    read -r -s -p "    Paste the Grafana service-account token: " GRAFANA_RENDER_TOKEN
    echo
fi
if [ -z "$GRAFANA_RENDER_TOKEN" ]; then
    echo "    No token provided. Re-run with GRAFANA_RENDER_TOKEN=glsa_... ./vault-setup.sh"
    exit 1
fi
# `vault kv put` against a KV v2 mount inserts the `data/` segment automatically.
vault kv put "${VAULT_KV_MOUNT}/${VAULT_KV_PATH}" token="$GRAFANA_RENDER_TOKEN"
echo "    OK"

cat <<DONE

Done. Next steps:
  - Apply the bridge manifests (or let ArgoCD sync them):
      kubectl apply -f .
  - Force a pod restart so the CSI driver materializes the secret:
      kubectl -n $NAMESPACE rollout restart deployment/grafana-slack-bridge
  - Confirm the synced k8s Secret exists:
      kubectl -n $NAMESPACE get secret grafana-bridge-render-token
  - Tail bridge logs after the next firing alert to confirm image rendering:
      kubectl -n $NAMESPACE logs -l app.kubernetes.io/name=grafana-slack-bridge -f
DONE
