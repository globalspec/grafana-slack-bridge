# syntax=docker/dockerfile:1
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 CompareNetworks, Inc.
FROM python:3.12-slim
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
