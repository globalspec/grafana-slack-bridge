# Local install (bare metal / VM)

Run the bridge directly on a Linux host as a systemd service. Useful for:

- Small deployments without Kubernetes
- Test / staging environments
- Servers already running Grafana where adding container infrastructure isn't worth it

## Prerequisites

- Linux host with systemd
- Python 3.12+
- A Slack bot token (`xoxb-…`) — see [docs/configuration.md](configuration.md#required-slack-bot-scopes)
- Network connectivity from Grafana to the host on the bridge's listening port

## Install

```bash
# 1. Clone (or copy a release tarball)
sudo git clone https://github.com/your-org/grafana-slack-bridge.git /opt/grafana-slack-bridge
cd /opt/grafana-slack-bridge

# 2. Create a dedicated user
sudo useradd -r -s /usr/sbin/nologin -d /opt/grafana-slack-bridge grafana-slack-bridge
sudo chown -R grafana-slack-bridge:grafana-slack-bridge /opt/grafana-slack-bridge

# 3. Set up the Python environment
sudo -u grafana-slack-bridge python3 -m venv /opt/grafana-slack-bridge/.venv
sudo -u grafana-slack-bridge /opt/grafana-slack-bridge/.venv/bin/pip install \
    -r /opt/grafana-slack-bridge/requirements.txt
```

## Configure

Create the config file at `/etc/grafana-slack-bridge.env` (mode `0600`, owned by `grafana-slack-bridge`):

```bash
sudo install -m 0600 -o grafana-slack-bridge -g grafana-slack-bridge \
    examples/config.env /etc/grafana-slack-bridge.env
sudo $EDITOR /etc/grafana-slack-bridge.env
```

Fill in at least `SLACK_BOT_TOKEN` and your channel. Full reference: [docs/configuration.md](configuration.md).

## Install the systemd unit

```bash
sudo install -m 0644 examples/systemd/grafana-slack-bridge.service \
    /etc/systemd/system/grafana-slack-bridge.service
sudo systemctl daemon-reload
sudo systemctl enable --now grafana-slack-bridge.service
```

Verify:

```bash
systemctl status grafana-slack-bridge.service
journalctl -u grafana-slack-bridge.service -f
curl http://localhost:8080/healthz
```

`/healthz` should respond `{"status":"ok","tracked_groups":0}`.

## Point Grafana at it

In Grafana, create a webhook contact point at:

```
http://<host>:8080/webhook?channel=<your-channel-id>
```

If Grafana and the bridge are on the same host and you've bound the bridge to `127.0.0.1` for security, use `http://127.0.0.1:8080/webhook?channel=…`.

See [docs/configuration.md](configuration.md#grafana-contact-point-configuration) for full Grafana provisioning.

## Hardening

By default the systemd unit binds to all interfaces (`0.0.0.0:8080`). For production:

### Bind to localhost only

Add to `/etc/grafana-slack-bridge.env`:

```
HOST=127.0.0.1
```

And update the unit to pass it to gunicorn — see the comments in `examples/systemd/grafana-slack-bridge.service`.

If Grafana is on a different host, terminate TLS + auth at a reverse proxy (nginx, Caddy) in front of the bridge.

### Restrict the service user

The provided systemd unit already enables hardening directives:

```ini
ProtectSystem=strict
ProtectHome=true
NoNewPrivileges=true
PrivateTmp=true
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_INET AF_INET6
```

Don't relax these unless you have a specific reason.

## Upgrading

```bash
cd /opt/grafana-slack-bridge
sudo -u grafana-slack-bridge git fetch --tags
sudo -u grafana-slack-bridge git checkout v0.X.Y
sudo -u grafana-slack-bridge /opt/grafana-slack-bridge/.venv/bin/pip install \
    -r /opt/grafana-slack-bridge/requirements.txt
sudo systemctl restart grafana-slack-bridge.service
```

A restart loses tracked alert groups (see [README.md](../README.md#what-this-doesnt-do)), so the next state change for any in-flight alert posts a new message instead of updating the original. Prefer to restart during a quiet window.

## Uninstalling

```bash
sudo systemctl disable --now grafana-slack-bridge.service
sudo rm /etc/systemd/system/grafana-slack-bridge.service
sudo rm /etc/grafana-slack-bridge.env
sudo rm -rf /opt/grafana-slack-bridge
sudo userdel grafana-slack-bridge
```
