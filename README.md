# grafana-slack-bridge

A small webhook receiver that turns Grafana alert notifications into **in-place updates** in Slack — one message per alert group, mutating through firing → resolved instead of spamming a new message on every state change.

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

![Example Slack alert produced by grafana-slack-bridge](docs/slack_message.png)

## The problem

Grafana has depreacated the OSS OnCall IRM.  OnCall has several nice abilities that are lacking in the native Grafana Slack alerting system.  In Grafana 12's native Slack contact point only calls `chat.postMessage`. Every state transition — firing, more hosts joining the group, resolved, repeat-interval re-notification — produces a brand-new Slack message. A noisy alert can generate dozens of identical messages in a single channel.

## What this does

Recreates some of the features loved about OnCall notifications.  It sits between Grafana and Slack. Grafana sends webhook payloads here. The bridge:

1. Extracts the alert `groupKey` from the payload
2. On first firing → calls `chat.postMessage` (or `files.upload_v2` if a panel image was rendered) and remembers `(groupKey → channel, ts)`
3. On subsequent state changes for the same `groupKey` → calls `chat.update`, mutating the original message with new content + a color-coded sidebar

Result: one Slack message per alert group through its entire lifecycle. Resolved alerts visibly turn green next to the original firing message.

Optionally, the bridge can fetch panel screenshots from Grafana's `/render` endpoint and upload them as Slack files — useful when Grafana's built-in screenshot pipeline doesn't reliably attach `imageURL` to webhook payloads (e.g. fleet-wide dashboards that time out).

## Features

- **In-place message updates** via Slack `chat.update`
- **Multi-channel routing** — append `?channel=Cxxxxx` to the webhook URL per Grafana contact point; one bridge process serves N channels
- **Optional image rendering** — bridge can call Grafana `/render` itself and upload PNGs as Slack files, bypassing Grafana's screenshot quirks
- **Severity-colored sidebars** — critical / warning / info / resolved use Slack's standard `attachment.color` for red / yellow / blue / green
- **Per-group serialization** — survives Grafana HA double-delivery without producing duplicate messages
- **Lightweight** — single ~600-line Python file, three runtime dependencies (Flask, gunicorn, requests)

## Quick start

Pick the installation guide that matches your environment:

| Environment | Guide |
|-------------|-------|
| Bare metal / VM (systemd + Python) | [docs/installation-local.md](docs/installation-local.md) |
| Docker / docker-compose | [docs/installation-docker.md](docs/installation-docker.md) |
| Kubernetes | [docs/installation-kubernetes.md](docs/installation-kubernetes.md) |

Then point a Grafana webhook contact point at `http://<bridge-host>:8080/webhook` — see [docs/configuration.md](docs/configuration.md).

## Configuration at a glance

| Env var | Required | Description |
|---------|----------|-------------|
| `SLACK_BOT_TOKEN` | yes | `xoxb-...` token with `chat:write`, `files:write`, `chat:write.public`, `files:read` |
| `SLACK_CHANNEL` | no | Default channel name (used when `?channel=` isn't on the webhook URL) |
| `SLACK_CHANNEL_ID` | no | Default channel ID (`Cxxxxx…`) — same purpose, but avoids a `conversations.list` lookup |
| `PORT` | no | Listen port (default `8080`) |
| `STATE_TTL_HOURS` | no | Drop idle groups after this many hours (default `24`) |
| `GRAFANA_URL` | no | Grafana base URL — required only for in-bridge image rendering |
| `GRAFANA_TOKEN` | no | Grafana service-account token with viewer access to the alerted dashboards |

Full reference: [docs/configuration.md](docs/configuration.md).

## Multi-channel routing

Per Grafana contact point, append the destination channel to the webhook URL:

```yaml
contactPoints:
  - name: "Infra alerts → #alerts-grafana"
    receivers:
      - type: webhook
        settings:
          url: http://grafana-slack-bridge:8080/webhook?channel=C012345ABCD
  - name: "Dev alerts → #alerts-devs"
    receivers:
      - type: webhook
        settings:
          url: http://grafana-slack-bridge:8080/webhook?channel=C098765WXYZ
```

Grafana's notification policy tree handles which rule goes to which contact point — no per-rule channel labels needed.

## Security model

`/webhook` is **unauthenticated** by default — anyone who can reach the port can post fake alerts. Intended usage is one of:

- Intra-cluster only (Kubernetes Service, no Ingress), or
- Behind a reverse proxy that adds authentication, or
- Bound to `127.0.0.1` and reached only via tunnel

Full threat model and hardening guidance: [SECURITY.md](SECURITY.md).

## How it compares to alternatives

- **Grafana's native Slack contact point** — works fine if you don't mind one message per state change.
- **Alertmanager + Slack receiver** — same `chat.postMessage`-only limitation as Grafana's native notifier.
- **PagerDuty / OpsGenie / Grafana OnCall** — full incident-management platforms; massive overkill if you just want tidy Slack alerts.
- **Robusta** — broader observability platform with Slack output; this bridge is a single file you can read in one sitting.

## What this doesn't do

- No interactive buttons (Ack / Resolve from Slack). Adding them needs a Slack Interactivity URL + a `/slack/interactive` endpoint — out of scope for this bridge.
- No durable state. Pod / process restart loses tracked groups; subsequent state changes start a new message. If you need durability across restarts, swapping the in-memory `dict` for Redis is ~30 lines.
- No deduplication beyond what Grafana's `groupKey` provides.

## Project status

Used in production. Active maintenance; semantic versioning.

## Contributing

Issues and PRs welcome. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

Apache License 2.0 — see [LICENSE](LICENSE).
