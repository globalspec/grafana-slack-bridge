# Contributing

Thanks for considering a contribution. The bridge is intentionally small — most changes should be small too.

## Reporting bugs

Open an issue with:

- Version (image tag or commit SHA)
- How you're running it (local, Docker, Kubernetes)
- Minimal Grafana webhook payload that reproduces the issue (redact tokens / hostnames)
- What you expected vs what happened
- Relevant log lines from the bridge

## Proposing features

Open an issue first to discuss. The bridge has a deliberately narrow scope — see "What this doesn't do" in [README.md](README.md). Some proposals are out of scope by design:

- Persistent state across restarts (suggest Redis support in code: open a PR with a swappable state backend)
- Interactive Slack buttons (Ack / Resolve)
- Routing logic beyond what the URL `?channel=` parameter provides

Things that fit the scope:

- New webhook authentication options
- Additional Slack API features (threading, reactions, etc.)
- More flexible templating for the message body
- Bug fixes and security hardening

## Development setup

```bash
git clone https://github.com/your-org/grafana-slack-bridge.git
cd grafana-slack-bridge
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Run locally:

```bash
export SLACK_BOT_TOKEN=xoxb-...
export SLACK_CHANNEL=alerts-grafana
python3 src/bridge.py
```

The bridge listens on `http://localhost:8080`. Send a sample webhook:

```bash
curl -X POST http://localhost:8080/webhook \
  -H 'Content-Type: application/json' \
  -d @examples/sample-webhook-payload.json
```

## Code style

- Python 3.12+
- Standard library + Flask + requests; avoid adding dependencies
- Keep `bridge.py` as a single file unless you have a specific reason to split
- Match the existing comment style: explain *why*, not *what*. Comments document non-obvious decisions, hidden constraints, and the reasoning behind unusual choices

## Testing

There is no automated test suite yet. Manual testing via a real Grafana → bridge → Slack flow is currently the bar. Contributions that add a test harness (pytest + mocked Slack/Grafana endpoints) are welcome.

When making changes that affect message formatting or webhook handling:

1. Run the bridge locally
2. POST a sample webhook (firing, then resolved) via `curl`
3. Verify the Slack message updates in place

## Pull requests

- Keep PRs focused. One feature / one bug fix per PR.
- Update [CHANGELOG.md](CHANGELOG.md) with a one-line entry under "Unreleased".
- If you change a config knob or env var, update [docs/configuration.md](docs/configuration.md) and any affected install guide.
- If you change security-relevant behavior, update [SECURITY.md](SECURITY.md).
- Sign-off your commits (`git commit -s`) — we follow the Developer Certificate of Origin.

## Releasing

(maintainers only)

The `.github/workflows/release.yml` workflow automates steps 4 below — pushing a tag triggers the multi-arch build and image publish to GHCR. The steps below describe the manual process for the rare case where you need to publish from a workstation.

1. Bump version in `CHANGELOG.md` (move "Unreleased" entries under the new version with date)
2. Tag: `git tag -s v0.X.Y -m "v0.X.Y"`
3. Push: `git push origin main --tags`  — this fires the release workflow
4. **(Manual fallback only — the workflow does this automatically.)** Build and push the multi-arch image:
   ```bash
   docker buildx build --platform linux/amd64,linux/arm64 \
     --annotation "index:org.opencontainers.image.source=https://github.com/your-org/grafana-slack-bridge" \
     --annotation "index:org.opencontainers.image.description=Grafana → Slack webhook receiver that does chat.update for in-place alert updates" \
     --annotation "index:org.opencontainers.image.licenses=Apache-2.0" \
     -t ghcr.io/your-org/grafana-slack-bridge:v0.X.Y \
     -t ghcr.io/your-org/grafana-slack-bridge:latest \
     --push .
   ```
   The `--annotation "index:…"` flags are essential for multi-arch images. Plain `LABEL` instructions in the Dockerfile only land in per-platform image configs — GHCR's UI reads `org.opencontainers.image.{source,description,licenses}` from the **manifest index** annotations to link the package back to its source repo, render a description, and display the license. Skip the `--annotation` flags and you get a package with no description and no repo link, even though `LABEL` entries exist in the Dockerfile.
5. Create a GitHub release with the changelog entry as the body
