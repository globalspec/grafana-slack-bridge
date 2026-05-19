# Security

## Reporting vulnerabilities

If you've found a security issue, please **do not open a public issue**. Use [GitHub's private vulnerability reporting](https://github.com/your-org/grafana-slack-bridge/security/advisories/new) to submit a report. Include:

- A description of the issue
- Steps to reproduce
- Affected version(s)
- Optional: a suggested fix

We'll acknowledge within 5 business days, agree on a coordinated-disclosure timeline, and credit you in the changelog once the fix is released (unless you'd rather stay anonymous).

## Supported versions

Only the latest minor release receives security fixes. Older versions may receive backports for critical issues on a best-effort basis.

## Threat model

The bridge is a small webhook receiver. Its design assumes:

| Trust boundary | Assumption | If violated |
|----------------|------------|-------------|
| `/webhook` callers | Only Grafana posts here | Attacker can spam fake alerts to Slack |
| Slack bot token | Only the bridge process reads `SLACK_BOT_TOKEN` | Attacker can post arbitrary messages as the bot |
| Grafana service-account token | Only the bridge reads `GRAFANA_TOKEN` | Attacker can read Grafana dashboards at the SA's permission level |
| Alert annotation content | Annotations are author-controlled by Grafana operators | Annotations get rendered as Slack mrkdwn, including links — a malicious annotation could include a phishing link styled as a "Dashboard" button |

The bridge does **not** authenticate `/webhook` callers and does **not** verify that an incoming payload actually came from Grafana. Run it on a network where only Grafana can reach it.

## Hardening recommendations

### 1. Don't expose `/webhook` to the public internet

The webhook endpoint accepts any well-formed JSON payload and posts it to Slack. If reachable by the internet:

- Anyone can send `{"groupKey": "x", "status": "firing", "title": "phish", ...}` and produce a real-looking Slack message
- Anyone can also exhaust your Slack rate limit budget

**Mitigations** (pick at least one):

- **Kubernetes**: keep the Service `ClusterIP` only. Do not create an Ingress / LoadBalancer. Optionally add a NetworkPolicy that allows traffic only from the Grafana namespace / pod selector.
- **Bare metal / Docker**: bind to `127.0.0.1` (`PORT=8080` + reverse proxy from localhost only) or put it on a private VLAN that Grafana can reach but the internet can't.
- **Public-internet deployment**: front the bridge with a reverse proxy (nginx, Caddy, Traefik) that enforces a shared-secret header. Grafana's webhook contact point supports `Authorization` headers via the `httpHeaders` map (Grafana 10.4+).

### 2. Minimize the Slack bot token scope

Required scopes:

- `chat:write` — post and update messages
- `chat:write.public` — post to channels the bot hasn't been invited to (optional, only if you do this)
- `files:write` — upload panel screenshots
- `files:read` — read share-message-ts back so `chat.update` can edit the file message

Do **not** grant `channels:read`, `groups:read`, `im:read`, `mpim:read`, or any `*:history` scope. The bridge doesn't need to read channel content or list channels — it learns channel IDs opportunistically from `chat.postMessage` responses and accepts channel IDs directly on the webhook URL.

### 3. Minimize the Grafana service-account token

If you use the in-bridge image renderer, the `GRAFANA_TOKEN` only needs **Viewer** role at the org level, scoped to the folders containing the dashboards your alerts reference. It does not need Editor or Admin.

### 4. Run the container non-root

The provided Dockerfile already sets `USER 1000:1000` and the example Kubernetes manifests set:

```yaml
securityContext:
  runAsNonRoot: true
  runAsUser: 1000
  readOnlyRootFilesystem: true
  allowPrivilegeEscalation: false
  capabilities:
    drop: [ALL]
```

Keep these in any custom deployment.

### 5. Pin and audit dependencies

Three direct runtime deps (`flask`, `gunicorn`, `requests`). Pin to exact versions in `requirements.txt`. Run `pip-audit` periodically.

### 6. Treat annotation content as untrusted

Alert rule annotations (description, summary, dashboard_url, runbook_url) are interpolated into Slack mrkdwn. Slack escapes most special characters, but link syntax (`<url|label>`) and channel mentions (`<!channel>`, `<!here>`) pass through. If your alert authors are not fully trusted, audit annotation content before deploying rules.

The bridge does not perform additional escaping on annotation content. This is a deliberate trade-off — escaping would break legitimate uses of Slack mrkdwn in annotations.

## Known limitations

- **No incoming request authentication.** Adding HMAC verification or shared-secret header support would be a small change, but is currently the operator's responsibility.
- **In-memory state.** A process restart loses tracked `(groupKey → ts)` mappings. The next state change for an in-flight group posts a new message instead of updating the original. No data loss; just a duplicate message.
- **Single process / single replica.** The in-memory state isn't shared across instances. Don't run with `replicas: > 1` unless you swap the state dict for Redis.
- **Logs may contain alert content.** The bridge logs `groupKey`, `ts`, status changes, and Slack API errors. These can include alert labels (hostnames, severities) and the rendered alert title. Treat bridge logs at the same sensitivity as the originating Grafana alert.

## Dependencies & supply chain

The container image is built from `python:3.12-slim`. Upstream Python base-image security advisories apply.

For air-gapped or supply-chain-sensitive deployments, build your own image from this source:

```bash
git clone https://github.com/your-org/grafana-slack-bridge.git
cd grafana-slack-bridge
docker build -t grafana-slack-bridge:local .
```
