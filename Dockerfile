# syntax=docker/dockerfile:1
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 CompareNetworks, Inc.
FROM python:3.12-slim

# OCI image labels — `org.opencontainers.image.source` is what GHCR uses to
# auto-link the package to its source repository. Without this label the
# package shows up unlinked on the GitHub UI.
LABEL org.opencontainers.image.source="https://github.com/globalspec/grafana-slack-bridge"
LABEL org.opencontainers.image.description="Grafana → Slack webhook receiver that does chat.update for in-place alert updates"
LABEL org.opencontainers.image.licenses="Apache-2.0"

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src/ ./src/
EXPOSE 8080
USER 1000:1000
# Single worker — the in-memory state dict is per-process, multiple workers
# would each have their own copy. Resolved-alert webhooks could land on a
# different worker than the firing webhook, miss the existing groupKey, and
# post a new message instead of updating. Threads (not processes) share state.
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "8", "src.bridge:app"]
