# 0001 — Portable Dockerized Stack, Multi-Workspace Single-User Tenancy & Webhook + In-App Notifications

Status: accepted 2026-11-01 (updated per 0004)

## Decision

TweetTracker runs as a self-contained `docker-compose.yml` stack (`web` Next.js/Sigma.js, `api` FastAPI/SSE, `worker` Python async scheduler, `db` PostgreSQL 16) portable across local workstation and any cloud Linux server. V1 notifications ship with two channels: **Real-Time In-App SSE** and **Outbound HMAC-Signed Webhooks**. Single-owner personal deployment gated by `TWEETTRACKER_ADMIN_PASSWORD`, supporting **multiple switchable Workspaces** (each with its own optional `my_x_handle`, custom $K \ge 1$ communities, polling cadences, and spend caps).

## What forced it

- Cloud-only serverless platforms (e.g., Vercel-only cron + function timeouts) fight continuous 5–10 minute polling workers and stateful SSE streams, whereas Docker Compose runs identically on `localhost` and a `$10–$20/mo` cloud VM (Fly, Hetzner, DigitalOcean).
- Python (`networkx`, `cdlib`, `scipy`, `scikit-learn`) owns the cleanest permissive BSD implementations for Seeded NMF, weighted $s$-core, SALSA, Burt's Structural Holes, and logistic calibration, while Next.js + `@react-sigma/core` owns 60fps WebGL graph rendering.
- A single generic Outbound Webhook connects immediately to n8n, Make, Zapier, Slack, Discord, or SMS/Telegram without maintaining 6 brittle channel integrations in Build 1.

## Consequences

- `docker compose up -d` starts the full product locally or on a remote server, inheriting secrets from host environment variables.
- Multi-user SaaS billing/RBAC and native Slack/Telegram OAuth apps stay out of Build 1 scope, while multi-workspace switching is supported out of the box via `workspace_id`.
