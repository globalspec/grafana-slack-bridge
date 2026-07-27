#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Standalone tests for the escalation logic (no pytest dependency).

Run:  python3 tests/test_escalation.py
Exits non-zero on any failure so it can gate CI.

Slack HTTP is stubbed — these cover the pure decision logic: RFC3339 parsing,
the age threshold, severity filtering, escalate-once, the master switch, the
in-channel vs dedicated-channel destination, and the resolve mirror.
"""
import os
import sys
import time
from datetime import datetime, timezone, timedelta

# Enable escalation before importing the module (config is read at import).
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test")
os.environ.update(
    ESCALATE_ENABLED="true",
    ESCALATION_CHANNEL_ID="CESC123",
    ESCALATE_AFTER_HOURS="4",
    ESCALATE_SEVERITIES="critical",
    ESCALATE_MENTION="<!here>",
)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import bridge  # noqa: E402

_calls = []


def _fake_slack_call(method, payload):
    _calls.append((method, payload.get("channel"), payload.get("text", "")))
    return {"ok": True, "ts": "111.222", "channel": payload.get("channel")}


bridge.slack_call = _fake_slack_call


def _iso_ago(hours):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _payload(sev, hours, status="firing"):
    return {
        "status": status,
        "commonLabels": {"severity": sev, "alertname": "X", "host": "H"},
        "commonAnnotations": {"summary": "disk full"},
        "alerts": [{"status": status, "labels": {"severity": sev, "host": "H"},
                    "annotations": {"description": "D:"}, "startsAt": _iso_ago(hours)}],
    }


def _seed(gk):
    bridge.state[gk] = {"ts": "1", "channel": "CMAIN", "last_update": time.time()}


def _reset():
    _calls.clear()
    bridge.state.clear()


_fails = []


def _check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        _fails.append(name)


# Parser
_check("parse Z", abs(bridge._parse_rfc3339("2026-07-27T12:00:00Z")
                      - datetime(2026, 7, 27, 12, tzinfo=timezone.utc).timestamp()) < 1)
_check("parse nanos", bridge._parse_rfc3339("2026-07-27T12:00:00.123456789Z") is not None)
_check("parse offset", bridge._parse_rfc3339("2026-07-27T12:00:00+00:00") is not None)
_check("parse zero-sentinel None", bridge._parse_rfc3339("0001-01-01T00:00:00Z") is None)

# critical firing 5h -> escalates once to dedicated channel
_reset(); _seed("gk1")
bridge.maybe_escalate(_payload("critical", 5), "gk1")
bridge.maybe_escalate(_payload("critical", 5), "gk1")  # second call must be no-op
_esc = [c for c in _calls if c[1] == "CESC123"]
_check("crit 5h escalates once", len(_esc) == 1)
_check("escalation mentions <!here>", "<!here>" in _esc[0][2])
_check("state marked escalated", bridge.state["gk1"].get("escalated") is True)

# critical firing 1h -> no escalation
_reset(); _seed("gk2"); bridge.maybe_escalate(_payload("critical", 1), "gk2")
_check("crit 1h no escalation", not any(c[1] == "CESC123" for c in _calls))

# warning 5h -> no escalation
_reset(); _seed("gk3"); bridge.maybe_escalate(_payload("warning", 5), "gk3")
_check("warning 5h no escalation", not any(c[1] == "CESC123" for c in _calls))

# master switch off -> nothing
_reset(); _seed("gk4"); bridge.ESCALATE_ENABLED = False
bridge.maybe_escalate(_payload("critical", 5), "gk4")
_check("disabled when ESCALATE_ENABLED false", len(_calls) == 0)
bridge.ESCALATE_ENABLED = True

# in-channel escalation: no dedicated channel -> post to the alert's own channel
_reset(); _seed("gk4b")
_saved = bridge.ESCALATION_CHANNEL_ID; bridge.ESCALATION_CHANNEL_ID = ""
bridge.maybe_escalate(_payload("critical", 5), "gk4b")
_check("in-channel escalates to alert channel (CMAIN)", any(c[1] == "CMAIN" for c in _calls))
bridge.ESCALATION_CHANNEL_ID = _saved

# resolve mirrors to the escalation message
_reset(); _seed("gk5")
bridge.state["gk5"].update(escalated=True, escalation_ts="111.222", escalation_channel="CESC123")
bridge.resolve_escalation(bridge.state["gk5"], _payload("critical", 5, "resolved"), "gk5")
_check("resolve updates escalation msg", any(c[0] == "chat.update" and c[1] == "CESC123" for c in _calls))

# ── Ack / Silence buttons ──────────────────────────────────────────────────
bridge.BUTTONS_ENABLED = True

_m = bridge.build_message(_payload("critical", 1), "gk-btn")
_acts = [b for b in _m.get("blocks", []) if b.get("type") == "actions"]
_check("buttons present on firing", len(_acts) == 1)
_ids = [e["action_id"] for e in _acts[0]["elements"]] if _acts else []
_check("ack + 3 silence buttons",
       _ids == ["alert_ack", "alert_silence_1h", "alert_silence_4h", "alert_silence_24h"])
_check("button value carries group_key",
       _acts and all(e["value"] == "gk-btn" for e in _acts[0]["elements"]))

_mr = bridge.build_message(_payload("critical", 1, "resolved"), "gk-btn")
_check("no buttons on resolved", not any(b.get("type") == "actions" for b in _mr.get("blocks", [])))

_gk = '{}/{slack="true",team="infra"}/{severity="critical"}:{alertname="Windows disk volume critically full", host="LA-2PPF01", severity="critical"}'
_mm = {x["name"]: x["value"] for x in bridge.matchers_from_group_key(_gk)}
_check("matcher alertname parsed", _mm.get("alertname") == "Windows disk volume critically full")
_check("matcher host parsed", _mm.get("host") == "LA-2PPF01")

# Ack click
_reset(); _seed("gkack")
bridge.handle_interaction({"type": "block_actions", "user": {"id": "U1", "username": "max"},
                           "channel": {"id": "C1"}, "container": {"message_ts": "9.9"},
                           "message": {"attachments": [{"color": "#E01E5A"}]},
                           "actions": [{"action_id": "alert_ack", "value": "gkack"}]})
_check("ack updates message", any(c[0] == "chat.update" and c[1] == "C1" for c in _calls))
_check("ack halts escalation + marks handled",
       bridge.state["gkack"].get("escalated") is True and bridge.state["gkack"].get("handled") is True)

# Silence click (mock the Grafana call)
_reset(); _seed("gksil")
_orig = bridge.create_grafana_silence
bridge.create_grafana_silence = lambda matchers, seconds, user: ("sid-123", None)
bridge.handle_interaction({"type": "block_actions", "user": {"id": "U1", "username": "max"},
                           "channel": {"id": "C1"}, "container": {"message_ts": "9.9"},
                           "message": {"attachments": [{"color": "x"}]},
                           "actions": [{"action_id": "alert_silence_4h", "value": "gksil"}]})
_check("silence updates message", any(c[0] == "chat.update" for c in _calls))
_check("silence marks handled", bridge.state["gksil"].get("handled") is True)
_check("handled note stored", str(bridge.state["gksil"].get("handled_note", "")).startswith(":mute:"))
bridge.create_grafana_silence = _orig
bridge.BUTTONS_ENABLED = False

print("\nRESULT:", "ALL PASS" if not _fails else f"{len(_fails)} FAILED: {_fails}")
sys.exit(1 if _fails else 0)
