# Configuration reference

All configuration is via environment variables (and the optional `?channel=` URL parameter). There is no config file.

## Environment variables

### Required

| Variable | Description |
|----------|-------------|
| `SLACK_BOT_TOKEN` | `xoxb-…` token from your Slack app. Required scopes listed below. |

### Defaults & channel resolution

The bridge needs to know which Slack channel to post to. Resolution order:

1. **`?channel=…`** on the incoming webhook URL — wins over everything else. Accepts either a channel ID (`C…`/`G…`/`D…`) or a channel name (`alerts-grafana`).
2. **`commonLabels.channel`** in the Grafana webhook payload — rule-level override (rarely used; URL routing is cleaner).
3. **`SLACK_CHANNEL`** env var — process-level default name.
4. If a name was resolved (not an ID) and `SLACK_CHANNEL_ID` is set, the bridge uses that ID directly — saving a `conversations.list` call.

| Variable | Default | Description |
|----------|---------|-------------|
| `SLACK_CHANNEL` | `alerts-grafana` | Channel name used when neither the URL nor payload override it. |
| `SLACK_CHANNEL_ID` | (unset) | Channel ID for the default channel. Seeds the channel-ID cache so the first firing alert can use the image-upload path without needing `channels:read` scope. Find this in Slack: right-click the channel → View channel details → Channel ID at the bottom. |

### Network / runtime

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `8080` | Port the HTTP server binds to. |
| `STATE_TTL_HOURS` | `24` | Drop tracked `(groupKey → channel, ts)` entries idle for longer than this. |

### Optional: bridge-rendered images

If set, the bridge will fetch panel screenshots directly from Grafana instead of relying on `imageURL` in the webhook payload.

| Variable | Description |
|----------|-------------|
| `GRAFANA_URL` | Base URL of your Grafana instance (e.g. `http://grafana:3000` or `https://grafana.example.com`). Internal cluster URL is fine. |
| `GRAFANA_TOKEN` | Grafana service-account token. Viewer role is sufficient. |

If either is unset, image rendering is skipped silently and the bridge falls back to `chat.postMessage` with the alert text only.

## Required Slack bot scopes

When creating the Slack app, request these OAuth scopes:

| Scope | Why |
|-------|-----|
| `chat:write` | Post and update messages |
| `chat:write.public` | Post in channels the bot hasn't been invited to (optional — invite the bot manually instead if you'd rather not grant this) |
| `files:write` | Upload panel screenshots |
| `files:read` | Read back the file-share message timestamp so `chat.update` can edit it later |

**Do not** request `channels:read`, `*:history`, or any other read scopes. The bridge doesn't need them.

After installing the app to your workspace, invite the bot to any channels you want it to post to (unless you granted `chat:write.public`):

```
/invite @grafana-alerts
```

## Required Grafana service-account permissions

(Only if using bridge-rendered images.)

Create a service account in Grafana with **Viewer** role at the org level. Generate a token for it — that's the value of `GRAFANA_TOKEN`.

If you scope dashboards to specific folders, ensure the service account has Viewer on each folder containing dashboards your alerts reference.

## Grafana contact-point configuration

The bridge replaces Grafana's native Slack contact point with a webhook contact point.

### Provisioning YAML

```yaml
apiVersion: 1
contactPoints:
  - orgId: 1
    name: "Slack (infra alerts)"
    receivers:
      - uid: slack-infra
        type: webhook
        disableResolveMessage: false
        settings:
          url: http://grafana-slack-bridge:8080/webhook?channel=C012345ABCD
          httpMethod: POST
```

### Notification policy routing

Route alerts by label, not by channel name:

```yaml
policies:
  - orgId: 1
    receiver: "Slack (infra alerts)"
    group_by: [alertname]
    routes:
      - receiver: "Slack (devs)"
        matchers:
          - team = "devs"
      - receiver: "Slack (dba)"
        matchers:
          - team = "dba"
```

Define each `Slack (…)` contact point with its own `?channel=` and you've got multi-channel routing with zero per-rule labels.

## What the Slack message looks like

The bridge produces a single message per alert group with:

- **Header** — `[FIRING:n]` or `[RESOLVED]` with the alert summary, color-coded by severity
- **Body** — per-alert sections separated by `— — —`, each containing:
  - Status emoji + host name (if labeled)
  - Description (from the alert rule's `description` annotation)
  - Severity, scope, and start time
  - Action links: Dashboard panel, Silence, Edit alert rule, Runbook (if present in annotations)
- **Image** — panel screenshot if rendered (bridge-rendered or from Grafana's webhook `imageURL`)
- **Color sidebar** — red (critical), yellow (warning), blue (info), green (resolved)

Alert rules can provide the following annotations to enrich messages:

| Annotation | Effect |
|------------|--------|
| `summary` | Title in the Slack message header |
| `description` | Body text under each alert section |
| `dashboard_url` | Dashboard panel link + screenshot source |
| `runbook_url` | Runbook link in the message footer |

And these labels:

| Label | Effect |
|-------|--------|
| `severity` | Sets color sidebar (`critical` / `warning` / `info`) and emoji |
| `host` | Shown inline next to the firing status |
| `scope` | Optional context shown in the metadata line |
