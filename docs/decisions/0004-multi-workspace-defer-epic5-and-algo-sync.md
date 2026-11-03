# 0004 — Multi-Workspace General Tool, Exact `vm-ranker` Tracer, Defer Epic 5, & Auto-Issue Algo Sync Watcher

Status: accepted 2026-11-01

## Decision

1. **General Multi-Workspace & Dynamic $K$-Community Model:** Remove all hardcoded `@gentoftech` and `AI / Startups / Small Business` assumptions. Support multiple switchable **Workspaces** (`workspaces` table), each with an optional `my_x_handle` (used for Story 1.4 Mutual/One-Way CRM and Epic 2 `20x` Mutual Reply vs. Quote guidance) and any number $K \ge 1$ of user-defined **Communities** (`communities` table) rendered on an **$N$-Pole Radial Bridge Map**.
2. **Defer Epic 5 to Build 2:** Hold off on Own-Account Flight Recorder (5.1), Own-Post Pattern Breakdown (5.2), and Pre-Post Draft Check (5.3) in Build 1 so Build 1 focuses strictly on the community graph, `x-algorithm` tracer, `15–60m` Breakout Radar (Reply vs. Quote strategy), 48h Scorecard/Tuner/Backtester (Epic 3), and Rising Creator Discovery (Epic 4).
3. **Exact `xai-value-model` + `vm-ranker` Tracer Port (`9106e9f`):** Port live `xai-org/x-algorithm` (`2026-10-31`) scoring mechanics (`xai-value-model/scoring.rs`, `vm-ranker/params.rs`, `0.75x` in-network reply/retweet rescore, `(1 - 0.25)*0.5^k + 0.25` diversity curve, `dpp_model.rs` `DppTheta = 0.65` semantic crowding, `OONRetweetReplyFilter`, `SelfReplyChainFilter`, `UserCredV2` log-mass scaling, and `SimClusters` `48h`) over Empirical-Bayes-shrunk per-impression propensities + calibrated latent-head surrogates.
4. **Automated Upstream `Algo Sync Watcher`:** Poll `xai-org/x-algorithm` commits daily against `last_synced_sha`, deterministically parse `param!(...)` diffs, summarize ranking/filter/scorer code diffs via the LLM router, and **automatically open deduplicated `algo-sync` GitHub Issues** on the configured GitHub repository (`GITHUB_TOKEN`).

## What forced it

- TweetTracker is a general-purpose personal intelligence tool; hardcoding one handle or a 3-circle triangle breaks whenever mapping a new market, brand, or $K \neq 3$ community set, whereas adding `workspace_id` + $N$-pole radial geometry upfront is minimal lift.
- Replying to `20x` mutuals and Quote-Posting OON breakouts on already-climbing `15–60m` threads captures high-leverage distribution immediately without cold-start risk; deferring Epic 5 strips 95% of LLM prompt surface area from Build 1 and lets Build 2 train on real 48h Postgres trajectories later.
- Live inspection of `xai-org/x-algorithm` (`9106e9f`, `2026-10-31`) showed xAI pushes automated sync commits almost nightly and moved final diversity, OON reply rescoring, and DPP deduplication into `vm-ranker/` + `xai-value-model/`. Without an automated watcher opening GitHub issues, local weights and filters would silently drift.

## Consequences

- **Build 1 Scope:** Multi-workspace switcher + Epics **1, 2, 3, and 4** + **`xai-value-model`/`vm-ranker` Tracer** + **Auto-Issue `Algo Sync Watcher`**.
- **Build 2 Backlog:** Epic 5 (`5.1–5.3`) plugs cleanly into the same `workspaces`, `posts`, and `algo_param_versions` tables once Build 1 is live.
- **Additional Secret Name (Optional for Cloud / Auto-Issues):** `GITHUB_TOKEN` (repo `issues:write` scope on your fork/repo; local dev can also fall back to authenticated `gh` CLI token if unset).
