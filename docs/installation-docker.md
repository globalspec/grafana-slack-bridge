# Docker install

## Prerequisites

- Docker 20.10+ (or any container runtime)
- A Slack bot token (`xoxb-…`) — see [docs/configuration.md](configuration.md#required-slack-bot-scopes)

## Quick run

```bash
docker run -d --name grafana-slack-bridge \
  --restart=unless-stopped \
  -p 8080:8080 \
  -e SLACK_BOT_TOKEN=xoxb-... \
  -e SLACK_CHANNEL=alerts-grafana \
  -e SLACK_CHANNEL_ID=C012345ABCD \
  ghcr.io/your-org/grafana-slack-bridge:v0.3.0
```

Verify:

```bash
curl http://localhost:8080/healthz
docker logs grafana-slack-bridge
```

Then point Grafana at `http://<docker-host>:8080/webhook?channel=<channel-id>` — see [docs/configuration.md](configuration.md#grafana-contact-point-configuration).

## docker-compose

A ready-to-edit `examples/docker-compose.yml` ships with the repo:

```yaml
services:
  grafana-slack-bridge:
    image: ghcr.io/your-org/grafana-slack-bridge:v0.3.0
    container_name: grafana-slack-bridge
    restart: unless-stopped
    ports:
      - "127.0.0.1:8080:8080"        # bind to localhost only — see hardening below
    environment:
      SLACK_CHANNEL: alerts-grafana
      SLACK_CHANNEL_ID: C012345ABCD
      STATE_TTL_HOURS: "24"
      # Optional: in-bridge image rendering
      # GRAFANA_URL: http://grafana:3000
    env_file:
      - ./grafana-slack-bridge.env    # contains SLACK_BOT_TOKEN (+ GRAFANA_TOKEN if used)
    read_only: true
    tmpfs:
      - /tmp
    user: "1000:1000"
    security_opt:
      - no-new-privileges:true
    cap_drop:
      - ALL
    healthcheck:
      test: ["CMD", "python3", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz', timeout=2)"]
      interval: 30s
      timeout: 3s
      retries: 3
```

Create the env file (mode `0600`):

```bash
cat > grafana-slack-bridge.env <<'EOF'
SLACK_BOT_TOKEN=xoxb-…
# Optional:
# GRAFANA_TOKEN=glsa_…
EOF
chmod 0600 grafana-slack-bridge.env
```

Bring it up:

```bash
docker compose up -d
docker compose logs -f
```

## Hardening

The compose example above already includes:

- `read_only: true` — root filesystem mounted read-only; `/tmp` is the only writable path (via tmpfs)
- `user: "1000:1000"` — non-root
- `cap_drop: [ALL]` — no Linux capabilities
- `no-new-privileges:true` — prevents privilege escalation
- `127.0.0.1:8080:8080` — bind only to localhost; reach via reverse proxy or tunnel

If Grafana is on a different host, terminate TLS + authentication at a reverse proxy (nginx, Caddy, Traefik). The bridge itself does not authenticate incoming webhooks.

## Building from source

If you want to build your own image (e.g. for supply-chain reasons, or to test changes):

```bash
git clone https://github.com/your-org/grafana-slack-bridge.git
cd grafana-slack-bridge
docker build -t grafana-slack-bridge:local .
```

Multi-arch build (requires `docker buildx`):

```bash
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  -t my-registry.example.com/grafana-slack-bridge:v0.3.0 \
  --push .
```

## Upgrading

```bash
docker compose pull
docker compose up -d
```

Or with plain `docker run`:

```bash
docker pull ghcr.io/your-org/grafana-slack-bridge:v0.X.Y
docker stop grafana-slack-bridge
docker rm grafana-slack-bridge
# re-run the docker run command with the new tag
```

A restart loses tracked alert groups; the next state change for any in-flight alert posts a new message. Prefer to restart during a quiet window.
