#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 CompareNetworks, Inc.
"""
grafana-slack-bridge — Grafana → Slack webhook receiver with chat.update.

Grafana's native Slack contact point only calls chat.postMessage — every
state change (firing → resolved, more alerts joining the group) becomes a
new Slack message. This bridge tracks (groupKey → channel+ts) and calls
chat.update instead, so a single message follows the alert group through
its lifecycle.

Grafana sends webhook payloads to /webhook. We:
  1. Parse groupKey from the payload (Grafana's group identity).
  2. Look up channel/ts in our in-memory state.
  3. If present → chat.update with new content + colored attachment.
  4. If absent → chat.postMessage, store channel/ts.
  5. If alert resolved → chat.update with green styling.

State is in-memory: lost on pod restart. Worst case after a restart is
the next state change posts a new message instead of updating — annoying
but not data loss. Acceptable trade-off vs. running Redis.

Configuration via env:
  SLACK_BOT_TOKEN     — xoxb-... (chat:write, files:write, chat:write.public)
  SLACK_CHANNEL       — default channel name (e.g. "alerts-grafana"), used
                        only when the webhook URL doesn't carry ?channel=
  PORT                — listen port (default 8080)
  STATE_TTL_HOURS     — drop tracked groups idle longer than this (default 24)

Escalation (re-surface long-unresolved alerts) — OFF unless ESCALATE_ENABLED:
  ESCALATE_ENABLED      — master switch (default false). When false, no
                        escalation logic runs and behaviour is unchanged.
  ESCALATION_CHANNEL_ID — Slack channel ID (Cxxxx) to escalate TO. Unset ⇒
                        escalate IN the alert's own channel (recommended
                        single-channel model): a fresh @here message there.
  ESCALATE_AFTER_HOURS  — a firing alert unresolved this long escalates (default 4)
  ESCALATE_SEVERITIES   — comma list of severities that can escalate (default "critical")
  ESCALATE_MENTION      — Slack mention prepended to escalations so they actually
                        push a notification (default "<!here>"; "<!channel>" for
                        everyone; "" disables the ping)

Interactive Ack / Silence buttons (Socket Mode) — OFF unless SLACK_APP_TOKEN:
  SLACK_APP_TOKEN       — Slack app-level token (xapp-...) with connections:write.
                        Enables Socket Mode (outbound WS) so button clicks reach
                        the bridge with no public ingress. Unset ⇒ no buttons.
  GRAFANA_SILENCE_TOKEN — Grafana service-account token with silence-write. The
                        Silence buttons no-op (log a warning) without it.

Per-contact-point channel routing:
  Add ?channel=Cxxxxxx (channel ID) or ?channel=name to the webhook URL on
  each Grafana contact point. This wins over SLACK_CHANNEL/SLACK_CHANNEL_ID,
  so a single bridge deployment serves N channels with N contact points and
  routing handled by Grafana's notification policy.
"""

import logging
import os
import re
import sys
import time
import threading
from collections import defaultdict
from typing import Any, Dict, Optional
from urllib.parse import urlparse, parse_qs, urlencode

import requests
from flask import Flask, request

# Flask's default logger is WARNING-only in production. Force INFO so successful
# webhook deliveries (post / update / etc.) are observable in kubectl logs.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _env(key: str, default: Optional[str] = None) -> str:
    value = os.environ.get(key, default)
    if value is None:
        sys.exit("ERROR: missing required env var " + key)
    return value


SLACK_TOKEN = _env("SLACK_BOT_TOKEN")
SLACK_CHANNEL = _env("SLACK_CHANNEL", "alerts-grafana")
# Optional: pass the channel ID (C-prefix) directly so we don't have to call
# conversations.list. The bot doesn't have channels:read in our install and we
# don't want to require it just for one ID lookup. If unset we'll opportunisti-
# cally learn the ID from the first successful chat.postMessage response (which
# returns it without needing the scope).
SLACK_CHANNEL_ID = os.environ.get("SLACK_CHANNEL_ID", "")
PORT = int(_env("PORT", "8080"))
STATE_TTL_SEC = int(_env("STATE_TTL_HOURS", "24")) * 3600

# ── Escalation config ──────────────────────────────────────────────────────
# When an alert of an escalated severity has been FIRING continuously for
# ESCALATE_AFTER_SEC without resolving, post a single fresh, mention-tagged
# message. The primary path does chat.update in place, which Slack never
# re-notifies for — so a critical that has been quietly edited for hours
# otherwise sinks unnoticed. Posting a NEW message both re-surfaces it (bottom
# of the channel) AND pings via the mention.
#
# Destination:
#   • ESCALATION_CHANNEL_ID unset (default) → escalate IN the alert's own
#     channel — a fresh @here/@channel message in the same place operators
#     already watch. This is the recommended single-channel model.
#   • ESCALATION_CHANNEL_ID set → escalate to that dedicated channel instead.
#
# OFF by default: escalation only runs when ESCALATE_ENABLED is truthy, so the
# stock deployment is byte-for-byte unchanged.
ESCALATE_ENABLED = os.environ.get("ESCALATE_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")
ESCALATION_CHANNEL_ID = os.environ.get("ESCALATION_CHANNEL_ID", "").strip()
ESCALATE_AFTER_SEC = int(os.environ.get("ESCALATE_AFTER_HOURS", "4")) * 3600
ESCALATE_SEVERITIES = {
    s.strip() for s in os.environ.get("ESCALATE_SEVERITIES", "critical").split(",") if s.strip()
}
# Prepended to escalation messages so Slack actually pushes a notification.
# "<!here>" pings active members; "<!channel>" everyone; "" = no ping.
ESCALATE_MENTION = os.environ.get("ESCALATE_MENTION", "<!here>").strip()

# ── Interactive Ack / Silence buttons (Socket Mode) ────────────────────────
# Buttons need Slack to deliver the click back to us. We use Socket Mode (an
# OUTBOUND WebSocket) so this in-cluster bridge needs no public ingress — just
# an app-level token (xapp-...). Enabled only when SLACK_APP_TOKEN is set.
#   • Ack     → marks the alert acknowledged and halts escalation.
#   • Silence → creates a Grafana silence (needs GRAFANA_SILENCE_TOKEN, a
#               service-account token with silence-write) and halts escalation.
# Buttons render on the chat.postMessage / escalation paths (not the
# file-upload image path — Slack file messages can't carry blocks).
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN", "").strip()
BUTTONS_ENABLED = bool(SLACK_APP_TOKEN)
GRAFANA_SILENCE_TOKEN = os.environ.get("GRAFANA_SILENCE_TOKEN", "").strip()
# Durations offered by the Silence buttons (label, seconds).
SILENCE_OPTIONS = [("1h", 3600), ("4h", 4 * 3600), ("24h", 24 * 3600)]

# Grafana render endpoint — used to fetch host-specific panel images for the
# firing alert. Grafana's built-in screenshot pipeline doesn't reliably attach
# imageURL to webhook payloads (it routes through a single-host panel without
# var-host injection and times out on fleet-wide queries), so we render here
# instead. Both env vars are optional — if either is missing we skip images.
GRAFANA_URL = os.environ.get("GRAFANA_URL", "http://grafana:3000").rstrip("/")
GRAFANA_TOKEN = os.environ.get("GRAFANA_TOKEN", "")

# Channel ID cache — Slack's files API requires channel_id (Cxxx), not name.
# Seeded from SLACK_CHANNEL_ID env if provided, also self-heals by recording
# the channel ID returned in chat.postMessage responses.
_channel_id_cache: Dict[str, str] = {}
if SLACK_CHANNEL_ID:
    _channel_id_cache[SLACK_CHANNEL] = SLACK_CHANNEL_ID

# Per-group_key locks — serialize concurrent webhooks for the same alert group
# so they don't both post a fresh message before either saves state. Grafana's
# HA deployment can deliver the same alert from multiple replicas in close
# succession (we've observed pairs landing ~1.5s apart).
_group_locks: Dict[str, threading.Lock] = defaultdict(threading.Lock)

# Severity → attachment color (Slack's `color` field is the vertical sidebar)
SEVERITY_COLOR = {
    "critical": "#E01E5A",
    "warning":  "#ECB22E",
    "info":     "#1264A3",
}
RESOLVED_COLOR = "#2EB67D"

# {groupKey: {ts: str, channel: str, last_update: float}}
state: Dict[str, Dict[str, Any]] = {}
state_lock = threading.Lock()

app = Flask(__name__)


# ──────────────────────────────────────────────────────────────────────────
# Slack API
# ──────────────────────────────────────────────────────────────────────────

_session = requests.Session()
_session.headers.update({
    "Authorization": "Bearer " + SLACK_TOKEN,
    "Content-Type":  "application/json; charset=utf-8",
})


def slack_call(method: str, payload: Dict) -> Dict:
    """POST to https://slack.com/api/<method>, return parsed JSON."""
    r = _session.post("https://slack.com/api/" + method, json=payload, timeout=15)
    try:
        data = r.json()
    except Exception:
        return {"ok": False, "error": "non-json response: " + r.text[:200]}
    if not data.get("ok"):
        app.logger.warning("slack %s error: %s", method, data)
    return data


# ──────────────────────────────────────────────────────────────────────────
# Grafana panel rendering
# ──────────────────────────────────────────────────────────────────────────

def _build_render_url(dashboard_url: str, host: Optional[str]) -> Optional[str]:
    """Turn a `dashboard_url` annotation into a `/render/d-solo/...` request URL.

    The alert rule's `dashboard_url` looks like:
       https://grafana.example.com/d/my-dashboard?viewPanel=5&var-host=web-01
    We extract the dashboard uid, the panel id (from viewPanel or panelId),
    and any var-* params, and re-issue under our internal GRAFANA_URL.
    """
    if not dashboard_url:
        return None
    parsed = urlparse(dashboard_url)
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2 or parts[0] not in ("d", "d-solo"):
        return None
    dash_uid = parts[1]
    slug = parts[2] if len(parts) >= 3 else dash_uid

    qs = parse_qs(parsed.query)
    panel_id = (qs.get("viewPanel") or qs.get("panelId") or [""])[0]
    if not panel_id:
        return None

    params = {
        "orgId": "1",
        "panelId": panel_id,
        "from": "now-1h",
        "to":   "now",
        "width":  "1200",
        "height": "500",
        # tv kiosk hides nav chrome
        "kiosk": "tv",
    }
    # Pass through any var-* template variables present in the annotation
    for k, v in qs.items():
        if k.startswith("var-") and v:
            params[k] = v[0]
    # Inject var-host from labels if not already specified
    if "var-host" not in params and host:
        params["var-host"] = host

    return GRAFANA_URL + "/render/d-solo/" + dash_uid + "/" + slug + "?" + urlencode(params)


def render_panel_png(payload: Dict) -> Optional[bytes]:
    """Render the firing alert's panel via Grafana, return PNG bytes or None."""
    if not GRAFANA_TOKEN:
        return None
    # Find the first firing alert that carries a dashboard_url annotation
    target = None
    for a in payload.get("alerts") or []:
        if a.get("status") != "firing":
            continue
        url = (a.get("annotations") or {}).get("dashboard_url")
        if url:
            target = a
            break
    if not target:
        return None

    host = (target.get("labels") or {}).get("host")
    render_url = _build_render_url(target["annotations"]["dashboard_url"], host)
    if not render_url:
        return None

    try:
        # Grafana's /render is synchronous — it blocks until the image renderer
        # service returns. 60s upper bound matches Grafana's own capture_timeout.
        r = requests.get(render_url, headers={
            "Authorization": "Bearer " + GRAFANA_TOKEN,
        }, timeout=60)
    except Exception as exc:
        app.logger.warning("render request error: %s", exc)
        return None
    if r.status_code != 200 or not r.headers.get("Content-Type", "").startswith("image"):
        app.logger.warning("render returned status=%s ct=%s body=%s",
                           r.status_code, r.headers.get("Content-Type"), r.text[:150])
        return None
    return r.content


def post_thread_image(png: bytes, channel_id: str, thread_ts: str) -> bool:
    """Upload the rendered panel PNG as a threaded reply under the alert message.

    The parent alert is a chat.postMessage (so it can carry the Ack/Silence
    buttons — Slack file messages can't hold blocks), and the image lives
    in-thread. Best-effort: returns True on success, False on any failure. A
    failure never affects the alert, which is already delivered.

    Slack upload flow: getUploadURLExternal → PUT bytes → completeUploadExternal
    (with channel_id + thread_ts to post it into the thread).
    """
    r = _session.get("https://slack.com/api/files.getUploadURLExternal", params={
        "filename": "panel.png", "length": str(len(png)),
    }, timeout=15)
    data = r.json()
    if not data.get("ok"):
        app.logger.warning("getUploadURLExternal failed: %s", data)
        return False
    file_id = data["file_id"]

    try:
        up = requests.post(data["upload_url"], data=png,
                           headers={"Content-Type": "application/octet-stream"},
                           timeout=60)
    except Exception as exc:
        app.logger.warning("thread image upload bytes error: %s", exc)
        return False
    if up.status_code >= 300:
        app.logger.warning("thread image upload status=%s body=%s", up.status_code, up.text[:150])
        return False

    c = _session.post("https://slack.com/api/files.completeUploadExternal", json={
        "files": [{"id": file_id, "title": "panel.png"}],
        "channel_id": channel_id,
        "thread_ts": thread_ts,
    }, timeout=15).json()
    if not c.get("ok"):
        app.logger.warning("thread image completeUploadExternal failed: %s", c.get("error"))
        return False
    return True


# ──────────────────────────────────────────────────────────────────────────
# Message formatting
# ──────────────────────────────────────────────────────────────────────────

def severity_emoji(severity: str, status: str) -> str:
    if status == "resolved":
        return ":white_check_mark:"
    return {
        "critical": ":rotating_light:",
        "warning":  ":warning:",
    }.get(severity, ":bell:")


def attachment_color(status: str, severity: str) -> str:
    if status == "resolved":
        return RESOLVED_COLOR
    return SEVERITY_COLOR.get(severity, SEVERITY_COLOR["info"])


def format_title(payload: Dict) -> str:
    status = payload.get("status", "firing")
    severity = (payload.get("commonLabels") or {}).get("severity", "info")
    summary = (payload.get("commonAnnotations") or {}).get("summary") \
        or payload.get("title") or "(no summary)"
    firing_count = sum(1 for a in payload.get("alerts", []) if a.get("status") == "firing")
    if status == "resolved":
        return f"{severity_emoji(severity, status)} [RESOLVED] {summary}"
    return f"{severity_emoji(severity, status)} [FIRING:{firing_count}] {summary}"


def format_alert_section(alert: Dict) -> str:
    labels = alert.get("labels") or {}
    annotations = alert.get("annotations") or {}
    host = labels.get("host")
    severity = labels.get("severity", "?")
    scope = labels.get("scope")
    description = annotations.get("description") or annotations.get("summary") or ""

    status_line = (":red_circle: *Firing*" if alert.get("status") == "firing"
                   else ":large_green_circle: *Resolved*")
    if host:
        status_line += f" · `{host}`"

    lines = [status_line, "", description, ""]

    meta = [f"*Severity:* `{severity}`"]
    if scope:
        meta.append(f"*Scope:* `{scope}`")
    starts_at = alert.get("startsAt", "")
    if starts_at:
        # Trim to "YYYY-MM-DD HH:MM UTC" for readability
        meta.append(f"*Started:* {starts_at[:16].replace('T', ' ')} UTC")
    lines.append(" · ".join(meta))
    lines.append("")

    # Action links
    if annotations.get("dashboard_url"):
        host_str = f" for {host}" if host else ""
        lines.append(f":chart_with_upwards_trend: <{annotations['dashboard_url']}|Dashboard panel{host_str}>")
    if alert.get("silenceURL"):
        lines.append(f":mute: <{alert['silenceURL']}|Silence>")
    if alert.get("generatorURL"):
        lines.append(f":gear: <{alert['generatorURL']}|Edit alert rule>")
    if annotations.get("runbook_url"):
        lines.append(f":book: <{annotations['runbook_url']}|Runbook>")

    return "\n".join(lines).strip()


def _action_buttons_block(group_key: str) -> Dict[str, Any]:
    """Top-level `actions` block: Acknowledge + Silence (1h/4h/24h). The
    group_key rides in each element's `value` so the interaction handler knows
    which alert was clicked. Kept at the message's top level (not inside the
    coloured attachment) so chat.update can cleanly swap it for an ack/silence
    note while leaving the attachment content intact."""
    elements = [{
        "type": "button",
        "action_id": "alert_ack",
        "text": {"type": "plain_text", "text": ":white_check_mark: Acknowledge"},
        "style": "primary",
        "value": group_key,
    }]
    for label, _sec in SILENCE_OPTIONS:
        elements.append({
            "type": "button",
            "action_id": f"alert_silence_{label}",
            "text": {"type": "plain_text", "text": f":mute: Silence {label}"},
            "value": group_key,
        })
    return {"type": "actions", "block_id": "alert_actions", "elements": elements}


def build_message(payload: Dict, group_key: Optional[str] = None) -> Dict[str, Any]:
    """Build the Slack API payload (excluding channel and ts).
    Uses the `attachments` field for the colored sidebar Slack pattern.
    When buttons are enabled and a firing group_key is supplied, adds a
    top-level Ack/Silence actions block."""
    status = payload.get("status", "firing")
    severity = (payload.get("commonLabels") or {}).get("severity", "info")
    color = attachment_color(status, severity)
    title = format_title(payload)

    alerts = payload.get("alerts") or []
    sections = "\n\n— — —\n\n".join(format_alert_section(a) for a in alerts)

    # Image: Grafana populates the screenshot URL per alert. Different code
    # paths use different field names — check several.
    image_url = None
    for alert in alerts:
        for key in ("imageURL", "image_url", "imageUrl"):
            v = alert.get(key)
            if v:
                image_url = v
                break
        if image_url:
            break

    attachment = {
        "color": color,
        "blocks": [
            {"type": "header",  "text": {"type": "plain_text", "text": title[:150]}},
            {"type": "section", "text": {"type": "mrkdwn", "text": sections[:2900]}},
        ],
    }
    if image_url:
        attachment["blocks"].append({
            "type": "image",
            "image_url": image_url,
            "alt_text": "Panel snapshot",
        })

    # Top-level `text` is used by Slack for notifications previews + accessibility
    msg: Dict[str, Any] = {
        "text": title,
        "attachments": [attachment],
    }
    # Ack/Silence buttons ride at the top level (not in the attachment) so they
    # can be swapped for an ack/silence note on click. Firing only.
    if BUTTONS_ENABLED and group_key and status == "firing":
        msg["blocks"] = [_action_buttons_block(group_key)]
    return msg


# ──────────────────────────────────────────────────────────────────────────
# Escalation
# ──────────────────────────────────────────────────────────────────────────

def _parse_rfc3339(ts: str) -> Optional[float]:
    """Parse Grafana's RFC3339 startsAt to an epoch float. None on failure or on
    the zero-time sentinel Grafana emits for an unset time (0001-01-01...)."""
    if not ts or ts.startswith("0001-01-01"):
        return None
    from datetime import datetime
    s = ts.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        # Belt-and-braces for odd fractional-second precision on older runtimes.
        import re as _re
        m = _re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?([+-]\d{2}:\d{2})?$", s)
        if not m:
            return None
        base, frac, off = m.group(1), (m.group(2) or "")[:7], (m.group(3) or "+00:00")
        try:
            return datetime.fromisoformat(base + frac + off).timestamp()
        except ValueError:
            return None


def firing_age_sec(payload: Dict) -> Optional[float]:
    """Longest continuous firing age (sec) in the group = now − earliest startsAt
    among currently-firing alerts. None if no firing alert carries a usable time."""
    now = time.time()
    ages = [now - t for a in (payload.get("alerts") or [])
            if a.get("status") == "firing"
            for t in [_parse_rfc3339(a.get("startsAt", ""))] if t is not None]
    return max(ages) if ages else None


def build_escalation_message(payload: Dict, age_sec: float,
                             group_key: Optional[str] = None) -> Dict[str, Any]:
    """Escalation variant of the normal message: a mention + 'unresolved Nh'
    banner prepended, forced critical colour. Mention goes in top-level `text`
    too so Slack actually pushes the notification. Carries the Ack/Silence
    buttons (the escalation is the prime place to act)."""
    base = build_message(payload, group_key)
    hours = age_sec / 3600.0
    banner = (ESCALATE_MENTION + " " if ESCALATE_MENTION else "") + \
        f":bangbang: *ESCALATED — unresolved for {hours:.1f}h*"
    att = dict(base["attachments"][0])
    att["color"] = SEVERITY_COLOR["critical"]
    att["blocks"] = [{"type": "section", "text": {"type": "mrkdwn", "text": banner}}] + \
        list(att.get("blocks", []))
    text = (ESCALATE_MENTION + " " if ESCALATE_MENTION else "") + base.get("text", "")
    out: Dict[str, Any] = {"text": text, "attachments": [att]}
    if base.get("blocks"):  # carry the Ack/Silence actions block through
        out["blocks"] = base["blocks"]
    return out


def maybe_escalate(payload: Dict, group_key: str) -> None:
    """Post a one-time escalation for a long-unresolved firing alert. No-op unless
    the feature is enabled. Call AFTER the primary send, with state unlocked.

    Destination is ESCALATION_CHANNEL_ID if set, else the alert's own channel
    (in-channel escalation): a fresh mention-tagged message that both re-surfaces
    the alert and pings, since chat.update alone is silent."""
    if not ESCALATE_ENABLED:
        return
    severity = (payload.get("commonLabels") or {}).get("severity", "")
    if severity not in ESCALATE_SEVERITIES:
        return
    age = firing_age_sec(payload)
    if age is None or age < ESCALATE_AFTER_SEC:
        return
    # Claim the escalation under lock so concurrent webhooks escalate once.
    with state_lock:
        entry = state.get(group_key)
        if entry is None or entry.get("escalated"):
            return
        entry["escalated"] = True
        target_channel = ESCALATION_CHANNEL_ID or entry.get("channel")
    if not target_channel:
        with state_lock:  # nothing to post to — release the flag for a retry
            e = state.get(group_key)
            if e is not None:
                e["escalated"] = False
        return
    post = slack_call("chat.postMessage",
                      {"channel": target_channel, **build_escalation_message(payload, age, group_key)})
    with state_lock:
        entry = state.get(group_key)
        if post.get("ok"):
            if entry is not None:
                entry["escalation_ts"] = post["ts"]
                entry["escalation_channel"] = post["channel"]
            app.logger.info("escalated group_key=%s age=%.1fh channel=%s ts=%s",
                            group_key, age / 3600.0, post.get("channel"), post.get("ts"))
        else:
            # Roll the flag back so a later firing cycle retries.
            if entry is not None:
                entry["escalated"] = False
            app.logger.warning("escalation post FAILED group_key=%s error=%s",
                               group_key, post.get("error"))


def resolve_escalation(entry: Dict, payload: Dict, group_key: str) -> None:
    """When an escalated alert resolves, edit its escalation message to the
    resolved (green) styling so the channel shows it cleared. Works for both the
    dedicated-channel and in-channel escalation models — keyed off whether an
    escalation message ts was recorded, not off ESCALATION_CHANNEL_ID."""
    ts = entry.get("escalation_ts")
    ch = entry.get("escalation_channel")
    if not ts or not ch:
        return
    upd = slack_call("chat.update", {"channel": ch, "ts": ts, **build_message(payload)})
    app.logger.info("escalation resolved group_key=%s ok=%s", group_key, upd.get("ok"))


# ──────────────────────────────────────────────────────────────────────────
# Interactive buttons (Ack / Silence) — Socket Mode
# ──────────────────────────────────────────────────────────────────────────

_LABEL_RE = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')


def matchers_from_group_key(group_key: str) -> list:
    """Reconstruct Grafana silence matchers from a Grafana groupKey. The instance
    labels live in the trailing `:{alertname="…", host="…", …}` segment. We
    silence on alertname (+ host if present) — i.e. this alert on this host."""
    seg = group_key
    idx = group_key.rfind(":{")
    if idx != -1:
        seg = group_key[idx + 2:]
    labels = {k: v for k, v in _LABEL_RE.findall(seg)}
    matchers = [{"name": n, "value": labels[n], "isRegex": False, "isEqual": True}
                for n in ("alertname", "host") if labels.get(n)]
    if not matchers:  # fallback: whatever labels we could parse
        matchers = [{"name": k, "value": v, "isRegex": False, "isEqual": True}
                    for k, v in labels.items()]
    return matchers


def create_grafana_silence(matchers: list, seconds: int, user: str):
    """Create a Grafana silence via the alertmanager API. Returns (silence_id, None)
    on success or (None, error_str) on failure."""
    if not GRAFANA_SILENCE_TOKEN:
        return None, "GRAFANA_SILENCE_TOKEN not configured"
    if not matchers:
        return None, "no matchers derived from group key"
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    body = {
        "matchers": matchers,
        "startsAt": now.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "endsAt": (now + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "comment": f"Silenced via Slack by {user}",
        "createdBy": f"slack:{user}",
    }
    try:
        r = requests.post(GRAFANA_URL + "/api/alertmanager/grafana/api/v2/silences",
                          headers={"Authorization": "Bearer " + GRAFANA_SILENCE_TOKEN,
                                   "Content-Type": "application/json"},
                          json=body, timeout=15)
    except Exception as exc:
        return None, str(exc)
    if r.status_code >= 300:
        return None, f"HTTP {r.status_code}: {r.text[:200]}"
    try:
        d = r.json()
    except Exception:
        return None, "non-json response"
    return d.get("silenceID") or d.get("silenceId"), None


def _mark_handled(group_key: str, note: str) -> None:
    """Record that an alert was acked/silenced: halt escalation and remember the
    note so subsequent Grafana re-fires (which chat.update the message) keep the
    note instead of restoring the buttons."""
    with state_lock:
        e = state.get(group_key)
        if e is not None:
            e["escalated"] = True   # blocks maybe_escalate
            e["handled"] = True
            e["handled_note"] = note


def _finalize_message(orig_message: Dict, note: str) -> Dict[str, Any]:
    """Rebuild a message: keep the coloured attachment content, replace the
    top-level actions block with a context note (ack/silence outcome)."""
    out: Dict[str, Any] = {
        "text": note,
        "blocks": [{"type": "context", "elements": [{"type": "mrkdwn", "text": note}]}],
    }
    att = orig_message.get("attachments")
    if att:
        out["attachments"] = att
    return out


def handle_interaction(payload: Dict) -> None:
    """Dispatch a Slack block_actions payload (an Ack or Silence button click)."""
    if payload.get("type") != "block_actions":
        return
    action = (payload.get("actions") or [{}])[0]
    action_id = action.get("action_id", "")
    group_key = action.get("value", "")
    u = payload.get("user") or {}
    uname = u.get("username") or u.get("name") or u.get("id") or "someone"
    who = f"<@{u['id']}>" if u.get("id") else uname
    channel = (payload.get("channel") or {}).get("id")
    message = payload.get("message") or {}
    ts = (payload.get("container") or {}).get("message_ts") or message.get("ts")
    if not channel or not ts:
        app.logger.warning("interaction missing channel/ts action=%s", action_id)
        return

    if action_id == "alert_ack":
        note = f":white_check_mark: *Acknowledged* by {who}"
        _mark_handled(group_key, note)
        slack_call("chat.update", {"channel": channel, "ts": ts, **_finalize_message(message, note)})
        app.logger.info("ack group_key=%s by=%s", group_key, uname)

    elif action_id.startswith("alert_silence_"):
        label = action_id.rsplit("_", 1)[-1]
        seconds = dict(SILENCE_OPTIONS).get(label)
        if not seconds:
            return
        sid, err = create_grafana_silence(matchers_from_group_key(group_key), seconds, uname)
        if sid:
            note = f":mute: *Silenced {label}* by {who}"
            _mark_handled(group_key, note)
            slack_call("chat.update", {"channel": channel, "ts": ts, **_finalize_message(message, note)})
            app.logger.info("silence group_key=%s dur=%s by=%s id=%s", group_key, label, uname, sid)
        else:
            app.logger.warning("silence FAILED group_key=%s dur=%s err=%s", group_key, label, err)
            slack_call("chat.postMessage", {"channel": channel, "thread_ts": ts,
                                            "text": f":warning: Silence failed: {err}"})


_socket_started = False
_socket_lock = threading.Lock()


def _start_socket_mode_once() -> None:
    """Open the Socket Mode WebSocket (outbound) so button clicks reach us with no
    ingress. No-op unless SLACK_APP_TOKEN is set. Reconnects are handled by the
    slack_sdk client."""
    if not BUTTONS_ENABLED:
        return
    global _socket_started
    with _socket_lock:
        if _socket_started:
            return
        _socket_started = True
    try:
        from slack_sdk.socket_mode import SocketModeClient
        from slack_sdk.socket_mode.response import SocketModeResponse
        from slack_sdk import WebClient
    except Exception as exc:
        app.logger.warning("slack_sdk unavailable — buttons disabled: %s", exc)
        return

    client = SocketModeClient(app_token=SLACK_APP_TOKEN, web_client=WebClient(token=SLACK_TOKEN))

    def _listener(cli, req):
        try:
            if req.type == "interactive":
                # Ack the envelope immediately (Slack requires <3s), then handle.
                cli.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
                handle_interaction(req.payload)
        except Exception as exc:  # never let a bad payload kill the listener
            app.logger.warning("socket interaction error: %s", exc)

    client.socket_mode_request_listeners.append(_listener)
    try:
        client.connect()
        app.logger.info("Socket Mode connected — Ack/Silence buttons enabled")
    except Exception as exc:
        app.logger.warning("Socket Mode connect failed: %s", exc)


# ──────────────────────────────────────────────────────────────────────────
# State garbage collection
# ──────────────────────────────────────────────────────────────────────────

def cleanup_loop():
    while True:
        cutoff = time.time() - STATE_TTL_SEC
        with state_lock:
            stale = [k for k, v in state.items() if v["last_update"] < cutoff]
            for k in stale:
                del state[k]
        if stale:
            app.logger.info("Cleaned up %d stale group entries", len(stale))
        time.sleep(300)


# ──────────────────────────────────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────────────────────────────────

@app.route("/healthz")
def healthz():
    with state_lock:
        return {"status": "ok", "tracked_groups": len(state)}, 200


@app.route("/webhook", methods=["POST"])
def webhook():
    payload = request.get_json(silent=True)
    if not payload:
        return {"error": "no JSON body"}, 400

    group_key = payload.get("groupKey") \
        or "/".join((payload.get("commonLabels") or {}).get(k, "") for k in ("alertname", "host", "severity"))
    if not group_key:
        return {"error": "no group key"}, 400

    # Resolution order for the destination channel:
    #   1. ?channel=... on the webhook URL (per-contact-point override)
    #   2. commonLabels.channel from the payload (rule-level override)
    #   3. SLACK_CHANNEL env default
    # Accepts either a channel ID (C-prefix) or a channel name.
    channel_override = request.args.get("channel", "").strip()

    # Serialize concurrent webhooks for the same alert group. Grafana's HA
    # deployment can emit the same notification from multiple replicas in
    # close succession; without this lock both can pass the empty-state check,
    # both post a new message, and the user sees duplicates.
    with _group_locks[group_key]:
        return _handle_webhook(payload, group_key, channel_override)


def _handle_webhook(payload: Dict, group_key: str, channel_override: str = "") -> Any:
    channel = channel_override \
        or (payload.get("commonLabels") or {}).get("channel") \
        or SLACK_CHANNEL
    msg = build_message(payload, group_key)
    status = payload.get("status", "firing")

    # If this alert was already acked/silenced, a Grafana re-fire must NOT restore
    # the buttons — keep the outcome note instead.
    if status == "firing" and "blocks" in msg:
        with state_lock:
            e = state.get(group_key)
            note = e.get("handled_note") if (e and e.get("handled")) else None
        if note:
            msg["blocks"] = [{"type": "context", "elements": [{"type": "mrkdwn", "text": note}]}]

    # Detect image presence for logging — useful when debugging missing screenshots
    has_image = any(
        a.get(k) for a in (payload.get("alerts") or []) for k in ("imageURL", "image_url", "imageUrl")
    )

    with state_lock:
        entry = state.get(group_key)
        existing = bool(entry)

    if entry:
        # Update existing message
        update = slack_call("chat.update", {
            "channel": entry["channel"],
            "ts":      entry["ts"],
            **msg,
        })
        if update.get("ok"):
            if status == "resolved":
                # Mirror the resolve into the escalation channel (if this group
                # was escalated) before we drop the state that holds its ts.
                resolve_escalation(entry, payload, group_key)
                # Evict immediately on resolve. A subsequent firing for the
                # same groupKey then takes the new-post path → fresh Slack
                # message → mobile/desktop notification. Without eviction,
                # the next firing would chat.update the now-resolved-styled
                # message in place, which Slack does NOT push a notification
                # for — the operator would miss the re-fire entirely.
                with state_lock:
                    state.pop(group_key, None)
            else:
                with state_lock:
                    state[group_key]["last_update"] = time.time()
            app.logger.info(
                "update ok status=%s group_key=%s ts=%s image=%s",
                status, group_key, entry["ts"], has_image,
            )
            # Long-unresolved firing → one-time escalation (no-op if not due).
            if status == "firing":
                maybe_escalate(payload, group_key)
            return {"action": "update", "ts": entry["ts"]}, 200

        # Update failed (message gone, channel renamed, etc.) — fall through to post.
        app.logger.warning(
            "chat.update failed (%s) group_key=%s, falling back to postMessage",
            update.get("error"), group_key,
        )
        with state_lock:
            state.pop(group_key, None)

    # ── New group: post the main message, then attach the panel image as a
    # threaded reply. The parent is always a chat.postMessage (NOT a file
    # upload) so it can carry the Ack/Silence buttons — Slack file messages
    # can't hold blocks. The rendered panel rides in-thread.
    post = slack_call("chat.postMessage", {"channel": channel, **msg})
    if not post.get("ok"):
        app.logger.warning("post FAILED status=%s group_key=%s error=%s",
                           status, group_key, post.get("error"))
        return {"action": "failed", "error": post.get("error")}, 502

    # Opportunistically learn the channel ID (Slack returns the C-prefix id even
    # when we addressed the channel by name) — needed to share the thread image.
    if post.get("channel") and not _channel_id_cache.get(channel):
        _channel_id_cache[channel] = post["channel"]
        app.logger.info("learned channel id name=%s id=%s", channel, post["channel"])
    with state_lock:
        state[group_key] = {
            "ts":          post["ts"],
            "channel":     post["channel"],
            "last_update": time.time(),
            "via":         "post",
        }
    app.logger.info("post ok status=%s group_key=%s ts=%s image=%s existing_state=%s",
                    status, group_key, post["ts"], has_image, existing)

    # Panel image as a threaded reply (firing only; best-effort — a render or
    # upload failure never affects the alert, which is already delivered).
    if status == "firing":
        try:
            png = render_panel_png(payload)
            if png:
                ok = post_thread_image(png, post["channel"], post["ts"])
                app.logger.info("thread image group_key=%s ok=%s", group_key, ok)
        except Exception as exc:
            app.logger.warning("thread image error group_key=%s: %s", group_key, exc)
        # Covers a long-firing alert reposting fresh after state loss but already
        # older than the escalation threshold.
        maybe_escalate(payload, group_key)
    return {"action": "post", "ts": post["ts"]}, 200


# Start the cleanup thread at module import time so it runs under both
# `python3 src/bridge.py` (dev) AND `gunicorn src.bridge:app` (prod).
# Previously this lived inside the `if __name__ == "__main__":` block
# below, which gunicorn never executes — the thread silently never
# started, state entries never aged out, and chat.update kept editing
# already-resolved messages forever instead of posting fresh ones.
#
# Guarded against double-start: gunicorn with --workers > 1 would import
# the module per worker; we only run --workers 1 today but the guard is
# cheap insurance for anyone who scales up after swapping the in-memory
# state dict for Redis.
_cleanup_started = False
_cleanup_lock = threading.Lock()

def _start_cleanup_once():
    global _cleanup_started
    with _cleanup_lock:
        if _cleanup_started:
            return
        _cleanup_started = True
        threading.Thread(target=cleanup_loop, daemon=True).start()

_start_cleanup_once()
_start_socket_mode_once()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
