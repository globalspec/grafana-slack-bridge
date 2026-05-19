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

Per-contact-point channel routing:
  Add ?channel=Cxxxxxx (channel ID) or ?channel=name to the webhook URL on
  each Grafana contact point. This wins over SLACK_CHANNEL/SLACK_CHANNEL_ID,
  so a single bridge deployment serves N channels with N contact points and
  routing handled by Grafana's notification policy.
"""

import logging
import os
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


def resolve_channel_id(name: str) -> Optional[str]:
    """Look up a Slack channel's id from its name. Cached forever per process.

    Lookup order:
      0. If `name` is already a channel ID (C/G/D-prefix), return as-is. This
         is the path used by per-contact-point ?channel=Cxxx routing.
      1. Cache (seeded from SLACK_CHANNEL_ID env + recent chat.postMessage replies)
      2. conversations.list — requires channels:read scope which our bot
         doesn't currently have. We still attempt it as a fallback so a future
         scope grant works without needing a code change.
    """
    if name and name[0] in ("C", "G", "D") and name[1:].isalnum() and name.isupper():
        return name
    if name in _channel_id_cache:
        return _channel_id_cache[name]
    cursor = ""
    while True:
        r = _session.get("https://slack.com/api/conversations.list", params={
            "exclude_archived": "true",
            "limit": "1000",
            "types": "public_channel,private_channel",
            "cursor": cursor,
        }, timeout=15)
        data = r.json()
        if not data.get("ok"):
            app.logger.warning("conversations.list failed: %s — set SLACK_CHANNEL_ID env to skip this", data)
            return None
        for c in data.get("channels", []):
            _channel_id_cache[c["name"]] = c["id"]
        if name in _channel_id_cache:
            return _channel_id_cache[name]
        cursor = data.get("response_metadata", {}).get("next_cursor") or ""
        if not cursor:
            return None


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


def upload_image_with_message(png: bytes, filename: str, channel_id: str,
                              initial_comment: str) -> Optional[Dict[str, str]]:
    """Slack files.upload_v2 flow.

    Returns a dict on any successful upload (the file message IS in the
    channel by then), or None only if the upload itself failed before Slack
    accepted it. The dict's keys:

      file_id : always present once the upload was accepted
      channel : the C-prefix channel id we shared to
      ts      : the share message timestamp, IF we could recover it via
                files.info — empty string otherwise

    A successful upload with `ts=""` means the message IS visible in Slack
    but we can't drive chat.update against it. The caller should still treat
    this as a delivered notification (no fallback chat.postMessage) — we just
    won't be able to coalesce future state transitions into the same message
    for this group. The most likely reason ts isn't recovered is a Slack
    scope gap (files.info shares field requires files:read + channel history
    scopes which our bot doesn't currently have).

    Three-step pattern per Slack docs:
      1) files.getUploadURLExternal — reserve an upload slot
      2) PUT bytes to the returned upload_url
      3) files.completeUploadExternal — share to a channel
      4) files.info — best-effort ts recovery (no longer fatal on miss)
    """
    # 1) reserve
    r = _session.get("https://slack.com/api/files.getUploadURLExternal", params={
        "filename": filename, "length": str(len(png)),
    }, timeout=15)
    data = r.json()
    if not data.get("ok"):
        app.logger.warning("getUploadURLExternal failed: %s", data)
        return None
    upload_url = data["upload_url"]
    file_id = data["file_id"]

    # 2) upload bytes (no auth header on the upload_url — token is in the URL)
    try:
        up = requests.post(upload_url, data=png,
                           headers={"Content-Type": "application/octet-stream"},
                           timeout=60)
    except Exception as exc:
        app.logger.warning("upload bytes error: %s", exc)
        return None
    if up.status_code >= 300:
        app.logger.warning("upload bytes status=%s body=%s", up.status_code, up.text[:200])
        return None

    # 3) complete + share into channel
    r = _session.post("https://slack.com/api/files.completeUploadExternal", json={
        "files": [{"id": file_id, "title": filename}],
        "channel_id": channel_id,
        "initial_comment": initial_comment,
    }, timeout=15)
    data = r.json()
    if not data.get("ok"):
        app.logger.warning("completeUploadExternal failed: %s", data)
        return None

    # From here on, the file is uploaded AND shared into the channel — the
    # message is visible to users regardless of what files.info returns.

    # 4) best-effort ts recovery so chat.update can later edit the comment.
    # Up to 5 tries × 0.4s = 2s budget. Shares may be empty on the first
    # response even after a successful complete; subsequent polls usually
    # populate it. If still empty after the budget, log the full file
    # object once and proceed without a ts.
    last_info = None
    for attempt in range(5):
        info = _session.get("https://slack.com/api/files.info",
                            params={"file": file_id}, timeout=15).json()
        last_info = info
        if not info.get("ok"):
            break
        shares = (info.get("file") or {}).get("shares") or {}
        for visibility in ("public", "private"):
            for ch, msgs in (shares.get(visibility) or {}).items():
                if msgs:
                    return {"file_id": file_id, "channel": ch, "ts": msgs[0].get("ts", "")}
        time.sleep(0.4)

    # Upload succeeded but ts couldn't be recovered. Log enough context to
    # diagnose the scope/timing issue without spamming.
    if last_info is not None:
        f = last_info.get("file") if last_info.get("ok") else None
        app.logger.warning(
            "upload ok but ts recovery missed file_id=%s files.info.ok=%s "
            "has_file=%s shares=%s (will deliver without chat.update support)",
            file_id, last_info.get("ok"),
            f is not None,
            ((f or {}).get("shares") or {}) if f else last_info.get("error"),
        )
    return {"file_id": file_id, "channel": channel_id, "ts": ""}


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


def build_message(payload: Dict) -> Dict[str, Any]:
    """Build the Slack API payload (excluding channel and ts).
    Uses the `attachments` field for the colored sidebar Slack pattern."""
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
    return {
        "text": title,
        "attachments": [attachment],
    }


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
    msg = build_message(payload)
    status = payload.get("status", "firing")

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
            with state_lock:
                state[group_key]["last_update"] = time.time()
            # When resolved, schedule eviction sooner so a re-fire posts new
            if status == "resolved":
                with state_lock:
                    # 30-minute lingering window; after that, re-fire is a new message
                    state[group_key]["last_update"] = time.time() - STATE_TTL_SEC + 1800
            app.logger.info(
                "update ok status=%s group_key=%s ts=%s image=%s",
                status, group_key, entry["ts"], has_image,
            )
            return {"action": "update", "ts": entry["ts"]}, 200

        # Update failed (message gone, channel renamed, etc.) — fall through to post.
        app.logger.warning(
            "chat.update failed (%s) group_key=%s, falling back to postMessage",
            update.get("error"), group_key,
        )
        with state_lock:
            state.pop(group_key, None)

    # ── New group, first firing: try render+upload-as-file path ─────────
    # We use Slack's files.upload_v2 + initial_comment so the file IS the
    # parent message — gives us in-channel image + an editable text block
    # for chat.update on subsequent state changes. We only attempt this on
    # firing transitions (resolved-first events have no useful current image).
    bridge_image_path_tried = False
    if status == "firing":
        png = render_panel_png(payload)
        if png:
            bridge_image_path_tried = True
            channel_id = resolve_channel_id(channel)
            if channel_id:
                comment_text = format_title(payload) + "\n\n" + \
                    "\n\n— — —\n\n".join(format_alert_section(a) for a in (payload.get("alerts") or []))
                up = upload_image_with_message(png, "alert.png", channel_id,
                                               comment_text[:2900])
                if up:
                    # Upload succeeded — the file message IS in the channel.
                    # ts may be "" if we couldn't recover the share message ts
                    # via files.info (likely a scope gap). In that case future
                    # state transitions for this group_key will land as fresh
                    # file messages rather than in-place updates — but we DO
                    # NOT also post a chat.postMessage here; the user already
                    # sees the alert + image once.
                    ts = up.get("ts") or ""
                    with state_lock:
                        state[group_key] = {
                            "ts":          ts,
                            "channel":     up["channel"],
                            "last_update": time.time(),
                            "via":         "file" if ts else "file-noupdate",
                        }
                    app.logger.info(
                        "post (file) ok status=%s group_key=%s ts=%s file_id=%s updatable=%s",
                        status, group_key, ts or "(unknown)", up["file_id"], bool(ts),
                    )
                    return {"action": "post-file", "ts": ts, "file_id": up["file_id"]}, 200
                app.logger.warning(
                    "file upload truly failed group_key=%s — falling back to chat.postMessage",
                    group_key,
                )

    # ── Fallback: plain chat.postMessage (no image) ────────────────────
    post = slack_call("chat.postMessage", {"channel": channel, **msg})
    if post.get("ok"):
        # Opportunistically learn the channel ID (Slack returns the C-prefix
        # ID in the response even when we addressed the channel by name).
        # The next firing-of-a-new-group attempt can use the upload-as-file
        # path without needing channels:read scope.
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
        app.logger.info(
            "post ok status=%s group_key=%s ts=%s image=%s existing_state=%s bridge_image_tried=%s",
            status, group_key, post["ts"], has_image, existing, bridge_image_path_tried,
        )
        return {"action": "post", "ts": post["ts"]}, 200

    app.logger.warning(
        "post FAILED status=%s group_key=%s error=%s",
        status, group_key, post.get("error"),
    )
    return {"action": "failed", "error": post.get("error")}, 502


if __name__ == "__main__":
    threading.Thread(target=cleanup_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
