# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.0] — 2026-07-27

### Added
- **Escalation for long-unresolved alerts.** The bridge coalesces an alert
  group into one message via `chat.update`, but Slack never re-notifies for an
  edit — so a critical that stays firing for hours quietly sinks down the
  channel and gets missed. When an alert of an escalated severity has been
  firing continuously for `ESCALATE_AFTER_HOURS` (default 4) without resolving,
  the bridge now posts a single fresh, mention-tagged message to a separate
  `ESCALATION_CHANNEL_ID`, and edits it to resolved styling when the alert
  clears. Off by default — no behaviour change unless `ESCALATION_CHANNEL_ID`
  is set. New env: `ESCALATION_CHANNEL_ID`, `ESCALATE_AFTER_HOURS`,
  `ESCALATE_SEVERITIES` (default `critical`), `ESCALATE_MENTION` (default
  `<!here>`). Firing age is derived from each alert's `startsAt`, so it works
  even across a bridge restart that lost in-memory state.

## [0.3.2] — 2026-05-19

### Fixed
- **State cleanup thread never started under gunicorn.** The thread that
  ages out tracked `(groupKey → ts)` mappings was started inside
  `if __name__ == "__main__":`, which gunicorn doesn't execute. State
  entries therefore lived forever. Cleanup thread now starts at module
  import time with a one-shot guard.
- **Re-fires after resolve were silent.** On resolve the bridge used to
  keep the state entry around for 30 minutes so flap re-fires would
  `chat.update` the same message. Slack doesn't push notifications on
  edits, so the re-fire appeared on-screen but the operator got no
  ping. The bridge now evicts state immediately on resolve — the next
  firing for the same groupKey takes the new-post path and produces a
  real notification. Grafana's `for:` clauses + group_interval already
  prevent sub-second flap-spam, so eviction-on-resolve is safe.

## [0.3.1] — 2026-05-19

### Security
- Bump `flask` 3.0.3 → 3.1.3 — fixes missing `Vary: Cookie` header on some
  session-accessor paths ([GHSA flask-vary-cookie](https://github.com/advisories)).
  Low severity for this project: the bridge does not use Flask sessions, but
  upgrading anyway to clear the Dependabot alert and stay current.
- Bump `requests` 2.32.3 → 2.33.0 — fixes two medium-severity advisories:
  - `.netrc` credential leak via attacker-controlled URLs (GHSA-9hjg-9r4m-mvj7)
  - Insecure temp-file reuse in `extract_zipped_paths()` (GHSA-9wx4-h78v-vm56)
  Low practical impact for the bridge (no `.netrc` usage, no zip extraction),
  but upgrading clears Dependabot and stays current.

## [0.3.0] — 2026-05-19

### Added
- Per-contact-point Slack channel routing via `?channel=Cxxxxx` URL query string.
  One bridge process can now serve N Slack channels by configuring N Grafana
  contact points, each with a different `?channel=` on its webhook URL.
- `resolve_channel_id` short-circuits when input matches a `C/G/D`-prefix Slack
  channel ID, avoiding a `conversations.list` call (which requires `channels:read`
  scope that the bridge doesn't otherwise need).

### Changed
- `SLACK_CHANNEL_ID` env var is now a fallback used only when the webhook URL
  omits `?channel=`. The URL parameter wins.

## [0.2.2] — 2026-05-18

### Fixed
- Don't fall back to `chat.postMessage` after a successful file upload when
  the file-share message timestamp couldn't be recovered. The file is already
  visible in the channel; a fallback post produced duplicate messages.

## [0.2.1] — 2026-05-17

### Added
- Per-`groupKey` `threading.Lock` to serialize concurrent webhook deliveries
  for the same alert group. Grafana HA deployments can deliver the same
  alert from multiple replicas within ~1.5s; without the lock both passed
  the empty-state check and posted duplicate messages.
- `SLACK_CHANNEL_ID` env var seeds the channel-ID cache, allowing the file-
  upload (image) path on the first firing of a new group without requiring
  `channels:read` scope.

## [0.2.0] — 2026-05-16

### Added
- Bridge can now render panel screenshots itself via the Grafana `/render`
  endpoint and upload them as Slack files via `files.upload_v2`. The upload
  becomes the parent message so `chat.update` works against it for later
  state transitions.
- `GRAFANA_URL` and `GRAFANA_TOKEN` env vars (optional — if unset, images
  are skipped and the bridge falls back to `chat.postMessage`-only).

## [0.1.1] — 2026-05-14

### Fixed
- Force `INFO` log level via `logging.basicConfig` so successful webhook
  deliveries are observable in container logs. Flask's default is `WARNING`
  in production, which made operational debugging painful.

## [0.1.0] — 2026-05-13

### Added
- Initial release. Bridges Grafana webhook contact points to Slack with
  `chat.update` for in-place message updates per alert group.

[Unreleased]: https://github.com/your-org/grafana-slack-bridge/compare/v0.3.2...HEAD
[0.3.2]: https://github.com/your-org/grafana-slack-bridge/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/your-org/grafana-slack-bridge/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/your-org/grafana-slack-bridge/compare/v0.2.2...v0.3.0
[0.2.2]: https://github.com/your-org/grafana-slack-bridge/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/your-org/grafana-slack-bridge/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/your-org/grafana-slack-bridge/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/your-org/grafana-slack-bridge/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/your-org/grafana-slack-bridge/releases/tag/v0.1.0
