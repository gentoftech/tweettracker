# Product Requirements Document (PRD): TweetTracker

Related docs: [`CONTEXT.md`](CONTEXT.md) | [`GLOSSARY.md`](GLOSSARY.md) | [`decisions/0004-multi-workspace-defer-epic5-and-algo-sync.md`](decisions/0004-multi-workspace-defer-epic5-and-algo-sync.md) | [`research/how-x-algo-works-today.md`](research/how-x-algo-works-today.md) | [`research/single-and-multi-community-graphs.md`](research/single-and-multi-community-graphs.md) | [`research/oss-breakdown.md`](research/oss-breakdown.md) | [`research/x-data-provider-pricing.md`](research/x-data-provider-pricing.md)

---

## 1. Product Vision & Locked Architecture

**Vision:** A general-purpose, multi-workspace X intelligence engine to map any set of user-defined communities ($K \ge 1$), catch breakout conversations in their first `15–60 minutes` while they are still climbing (guiding **Reply (`20x` Mutual)** vs. **Quote-Post (OON-safe)** execution to maximize reach and follower growth per reply), continuously sharpen our `x-algorithm` tracer against 48-hour outcomes, spot `<50k` rising creators via peer co-signs, and automatically track upstream changes in [`xai-org/x-algorithm`](https://github.com/xai-org/x-algorithm) (verified against `HEAD = 9106e9f`, Oct 31, 2026).

### Locked Technical & Scope Decisions
* **Portable Dockerized Stack (`docker-compose.yml` — `decisions/0001`):** Runs identically on `localhost` or any cloud Linux VM/server:
  * **`web`:** Next.js 15 (TypeScript, Tailwind CSS, shadcn/ui, `Sigma.js` + `graphology` WebGL single-community & $N$-pole radial bridge maps, trajectory & calibration charts).
  * **`api`:** Python FastAPI serving REST + Server-Sent Events (SSE) for real-time radar streaming and in-app alerts.
  * **`worker`:** Python async scheduler running delta post ingestion, trajectory snapshotting, behavioral graph + reply-yield core recomputation, `xai-value-model`/`vm-ranker` tracing, 48h outcome grading, calibration jobs, upstream `xai-org/x-algorithm` sync checks, and outbound webhooks.
  * **`db`:** PostgreSQL 16 storing workspaces, custom communities, graph edges, own-account reply outcomes & follower snapshots, time-series post snapshots, versioned `x-algorithm` parameters, predictions, upstream sync diffs, and API spend ledgers.
* **Account-Agnostic, Circle-Free Multi-Workspace Design (`decisions/0004` + 2026-11-02 X-Native Simplification):**
  * **Workspace Switcher:** Create and switch between multiple isolated **Workspaces** (e.g., personal brand, company/venture, or pure market research) with zero manual "Circle" taxonomy setup.
  * **Optional `my_x_handle` per Workspace:** Enter any `@handle` to enable Story 1.4 Mutual/One-Way CRM, Radar `20x` Mutual Reply vs. Quote-Post guidance, and per-account Empirical Bayes reply-yield + follower-conversion learning, or leave blank for pure external ecosystem mapping.
  * **Emergent Auto-Clustering & Native X Lists:** Accounts are imported directly via `@handles`, CSV, or X Lists (`x_list_ids_json`); the force-directed WebGL graph positions and colors connected accounts automatically via Louvain modularity over 14d-decayed follow/reply/quote edges (while retaining backwards-compatible `communities` DB schema under the hood).
* **Build 1 vs. Build 2 Boundary (`decisions/0004`):**
  * **Build 1 (Core Graph, Tracer, Radar, Calibration, Rising & Algo Watcher):** Ships **Epics 1, 2, 3, and 4** + **Exact `xai-value-model`/`vm-ranker` Tracer** + **Daily Auto-Issue `Algo Sync Watcher`**.
  * **Deferred to Build 2:** **Epic 5** (Own-Account Hour-1 Flight Recorder, Own-Post Pattern Breakdown, and Pre-Post Draft Check) once Build 1 has accumulated calibrated 48h trajectory baselines.
* **Notifications (`decisions/0001`):** **Real-Time In-App SSE** alerts + **Generic Outbound HMAC-SHA256 Webhooks** (n8n, Make, Zapier, Slack, Discord, etc.).
* **Pluggable `XDataProvider` + Offline `FixtureProvider` (`decisions/0003`):** Router supporting `tweetapi_com`, `socialdata`, `twitterapi_io`, `official_x`, and `fixture`. **Build 1 ships `TweetAPI.com` (`TWEETAPI_API_KEY`) and `FixtureProvider`** active (`200`-ID batch snapshots, `<=512`-char `from:` + `list:<id>` search, `200`/pg `following-list`, `1`-unit `user/tweets-and-replies` own-reply yield sync, free `/tw-v2/account/usage` reconciliation).
* **5-Provider LLM Router (`decisions/0002` & `0004`):** Supports `xai_grok`, `google_gemini`, `openai`, `anthropic`, and `deepinfra`. Build 1 wires **Grok (`XAI_API_KEY`)** and **Gemini (`GOOGLE_AI_API_KEY`)** for Upstream `Algo Sync Watcher` Rust diff summaries (core Graph/Radar/Backtester is 100% deterministic with `$0` LLM polling cost).
* **Secrets Discipline:** Configured via the encrypted `/settings` vault or inherited from OS/platform environment variables—never committed to `.env` files or repo configs.

---

## 2. Build 1 Epics & User Stories

### Epic 1: Dynamic Community Network Graph
**Purpose:** Map any workspace's $K \ge 1$ target communities on X so we see who holds real conversational influence, how communities overlap ($K \ge 2$), and where to focus two-way relationship-building.

#### Story 1.1 — Curate Workspace Communities & Target Account Lists
* **User Story:** As a user, I want to create custom communities in a workspace and group X accounts into them so that the system knows which voices define my target ecosystem.
* **Scope:**
  * Create/edit/reorder $K \ge 1$ custom **Communities** per workspace (e.g., `AI`, `Startups`, `Small Business`—or any arbitrary domain set) with custom colors and optional X List IDs (`list:<id>`).
  * Add bootstrap accounts via `@handle`/URL, bulk paste/CSV import, or X List sync (`SEED_BOOTSTRAP_GRACE_DAYS = 28` bootstrap grace period before empirical reply-yield / dormancy pruning applies).
  * Multi-community manual tags paired with **$K$-Component Behavioral Mini-SimClusters (Seeded NMF)** to compute `KnownFor` anchor weights and $K$-dimensional `InterestedIn` overlap vectors from active audience co-engagement.
  * Hydrates profile metadata, 30d posting cadence, and age-matched baseline curves.
* **Acceptance Criteria:** Supports multiple workspaces and arbitrary $K \ge 1$ communities; bulk import deduplicates handles, hydrates baselines, and populates the Network Graph.

#### Story 1.2 — Identify the Real Heavy Hitters in Each Community
* **User Story:** As a user, I want to see which accounts in my lists actually drive reach and conversation when they engage—rather than just who has the most followers—so that I know whose interactions move the needle.
* **Scope:**
  * Computes `0–100` **Heavy Hitter Score ($H_u$)** per community from: (1) **Downstream Catalysis Lift** (observed 60m velocity acceleration on tracked posts after $u$ replies/quotes), (2) **Weighted $s$-Core Depth, SALSA Authority & `UserCredV2`-Style Log-Mass** (`clamp(165.2 + 7.07 * ln(mass), 0, 100)`) on the 14d-decayed behavioral graph, and (3) **Own-Post Tracer Score**.
  * Interactive Single-Community WebGL graph + sortable leaderboard with toggles for **Heavy Hitter Score**, **Reply Yield**, and **Raw Followers**.
* **Acceptance Criteria:** Sort/filter any community by Heavy Hitter Score, Reply Yield, Catalysis Lift, or Follower Efficiency, with drill-down into catalyzed threads.

#### Story 1.3 — Spot the "Bridge" Accounts Across Communities ($K \ge 2$)
* **User Story:** As a user, I want to see which accounts sit at the intersection of two or more communities so that I know who can carry a conversation from one circle into another.
* **Scope:**
  * Computes **Bridge Score ($B_u$)** combining **$K$-Normalized Guimerà–Amaral Participation Coefficient ($\tilde{P}_u = \frac{K}{K-1}[1 - \sum_{c=1}^{K}(w_{u,c}/w_u)^2]$)** and **Burt's Structural Hole Brokerage ($1 - C_u$)**, classifying accounts as *Provincial Heavy Hitters*, *2-Way Bridges*, or *Multi-Circle Connectors*.
  * Renders an **$N$-Pole Radial Bridge Map** placing the workspace's $K$ community anchors evenly around a unit circle ($K=2$ axis, $K=3$ triangle, $K=4+$ polygon) so Bridges visually sit along connecting chords and the center (automatically hidden when $K = 1$).
* **Acceptance Criteria:** Clicking a Bridge displays its % split across all $K$ workspace communities and recent cross-community threads.

#### Story 1.4 — Track Mutual vs. One-Way Relationships (When `my_x_handle` Is Configured)
* **User Story:** As a user, I want to see which key accounts in my network follow my configured handle back and engage with me versus which ones are still one-way, so that I can prioritize building two-way relationships.
* **Scope:**
  * Relative to the workspace's `my_x_handle` (cleanly hidden/optional if `my_x_handle` is blank), classifies every tracked account across 5 states grounded in X's `+15.0` `BidirectionalFollowReplyWeightBoost` (`20.0` total reply weight on `!is_reply && !is_retweet` posts):
    1. **Mutual — Active (`20x` Live)** | 2. **Mutual — Dormant** | 3. **One-Way Outbound — Warming** | 4. **One-Way Outbound — Cold** | 5. **Inbound Unreciprocated** (1-click follow-back unlocks `20x`).
  * Ranks `Warming`, `Dormant`, and `Inbound Unreciprocated` accounts in a prioritized **Relationship ROI Queue** ($H_u \times B_u \times \text{ReciprocationReadiness}$).
* **Acceptance Criteria:** Filterable 5-state relationship table and ROI queue updating dynamically per workspace `my_x_handle`.

---

### Epic 2: Two-Stage `x-algorithm` Tracer & Breakout Post Radar
**Purpose:** Trace `xai-org/x-algorithm` (`9106e9f`) mechanics on tracked posts in minutes `15–60` so we catch climbing threads early and choose the highest-leverage engagement play (**Reply** vs. **Quote-Post**).

#### Story 2.1 — Live Feed of Early Breakouts (`xai-value-model` + `vm-ranker` Tracer)
* **User Story:** As a user, I want a real-time feed of posts from my tracked accounts outperforming that author's normal baseline in their first `15–60 minutes`, ranked to maximize expected reach per reply, with clear Reply vs. Quote guidance grounded in the live X algorithm.
* **Scope:**
  * Configurable paced polling detects new posts and captures snapshots across `15m, 30m, 45m, 60m` (and `2h, 6h, 24h, 48h` for Epic 3).
  * **Two-Stage `x-algorithm` Tracer (`9106e9f` Port):**
    1. **Stage 1 — Filter & Retrieval Velocity:** Evaluates `OONRetweetReplyFilter`, `SelfReplyChainFilter`, `AgeFilter <= 48h`, Cold-Start Thompson window (`<50k` followers, `<=2h`, `<200` views), Author Age-Matched Velocity Z-Score, Multi-Signal SEISMIC cascade infectiousness, and Heavy-Hitter Catalysis.
    2. **Stage 2 — `xai-value-model` + `vm-ranker` Expected Rank Score:** Estimates Empirical-Bayes-shrunk per-impression propensities ($\hat{p}_a = \text{count}_a / \text{views}$) + calibrated latent-head surrogates (`bookmarks/views` + post length/media → copy-link/dwell heads), runs an **exact port of `xai-value-model/scoring.rs` + `vm-ranker/params.rs`** (`offset_score`, `OonWeightFactor = 0.75` on OON + in-network replies/retweets, `(1 - 0.25)*0.5^k + 0.25` author diversity, and **`dpp_model.rs` `DppTheta = 0.65` semantic crowding** check against active circle posts).
  * **Expected Reply Reach Ranking & Tactical Callout:** Live `Hot Posts` (`live_queue=true`) sorts climbing threads by `expected_reply_reach_score` (blending `breakout_score`, per-author empirical `yield_score`, projected post view scale, and `20x` mutual lift). Recommends **Reply (`20x` Mutual Boost)** when `my_x_handle` mutually follows the author on an original/quote post, or **Quote-Post (OON-Safe)** when non-mutual (bypassing `OONRetweetReplyFilter` and the `0.75x` reply rescore), recording `acted_action_kind` (`"reply"` | `"quote"`) on click and surfacing per-author `~avg views/reply · +follows/30d`.
* **Acceptance Criteria:** Auto-updating SSE feed ranking `15–60m` breakouts by expected reply reach with tracer breakdown, author reply-yield stats, DPP novelty badge, and Reply/Quote guidance.

#### Story 2.2 — Filter Breakouts by Community & Bridge Status
* **User Story:** As a user, I want to filter the breakout feed by any workspace community or Overlapping/Bridge accounts, so that I can jump into relevant conversations fast.
* **Scope:** Dynamic tabs (`All`, each custom workspace community $c_1 \dots c_K$, `Bridges / Cross-Community`) + toggles (`Mutuals Only`, age `<30m` / `<60m` / `<2h`) + saved filter presets.
* **Acceptance Criteria:** Sub-150ms filter transitions isolating single-community and Bridge breakouts accurately.

#### Story 2.3 — Instant Breakout Notifications
* **User Story:** As a user, I want to get alerted when a tracked post starts taking off rapidly, so that I can catch major conversations without watching a dashboard all day.
* **Scope:** **In-App SSE Alerts** (toast + notification center + optional chime) and **Outbound HMAC-SHA256 Webhooks** (`breakout.surge`, `breakout.watch`) with per-post tier deduplication, per-community threshold sliders, quiet hours, hourly caps, **Send Test Webhook**, and retry audit log.
* **Acceptance Criteria:** Verified end-to-end In-App and HMAC Webhook delivery with deduplication and audit logs.

---

### Epic 3: Self-Improving Prediction & Backtesting Loop
**Purpose:** Grade early Radar calls at **48 hours** (matching X's `thunder/` & `SimclustersMaxCandidateAgeHours = 48` ceiling), calibrate latent-head/threshold weights automatically, and backtest candidate rules against stored trajectories.

#### Story 3.1 — Automatic 48-Hour Prediction Scorecard
* **User Story:** As a user, I want the system to automatically check where each tracked post ended up after its 48-hour lifecycle and grade its own early predictions, so that we know how accurate our breakout alerts and tracer really are.
* **Scope:** Labels 48h outcomes ($\ge 3.0\times$ author median 48h reach + community impression floor) and computes rolling **Precision**, **Recall**, **$F_{0.5}$**, **Brier Calibration Curve**, **Median Lead-Time Upside**, and **False Alarms / Missed Breakouts** postmortem tables.
* **Acceptance Criteria:** Automated 48h grading updates workspace and per-community scorecards with post-level drill-downs.

#### Story 3.2 — Continuous Tracer & Threshold Calibration
* **User Story:** As a user, I want the system to learn from its past hits and misses across my tracked accounts, so that its breakout probability score gets more accurate without manual tweaking.
* **Scope:** Automated $L_2$-regularized calibration job (shrunk toward `xai-value-model` / `vm-ranker` priors once $\ge 100$ matured trajectories exist) tuning latent-head surrogate weights and per-community thresholds under **max `±20%` single-run drift** and **chronological holdout gating**, logging every promotion/skip to a `BIDIRECTIONAL_BOOST_CHANGE`-style **Parameter Changelog** with 1-click rollback.
* **Acceptance Criteria:** Version-stamped predictions, holdout-gated promotion, human-readable changelog, and instant rollback.

#### Story 3.3 — Historical Backtesting Sandbox
* **User Story:** As a user, I want to test new breakout rules or upstream `param.rs` changes against my collected history of post trajectories before changing live alerts.
* **Scope:** Sandbox UI replaying custom weights/thresholds/filters against stored PostgreSQL `15m–48h` post snapshots, displaying side-by-side `Live` vs. `Candidate` Precision, Recall, Alerts/Day, Newly Caught Hits, Newly Filtered Fizzles, and **Promote to Live**.
* **Acceptance Criteria:** Replays up to 10,000 stored trajectories in `<2 seconds` with post-level classification diffs.

---

### Epic 4: Rising Influencer Discovery ("Influencers in the Making")
**Purpose:** Spot smaller accounts (`< 50,000` followers, aligned with `ColdStartFollowerCap = 50,000`) consistently punching above their follower count and earning early engagement from our communities' Heavy Hitters and Scouts.

#### Story 4.1 — Rising Voices Leaderboard
* **User Story:** As a user, I want a ranked view of smaller accounts whose posts repeatedly reach many times their follower count, so that I can discover and connect with up-and-coming creators early.
* **Scope:** Harvests `<50k`-follower candidates from 2-hop replies, quotes, mentions, and new follows of Epic 1 accounts; ranks by **Punch-Above-Weight Score ($R_c$)** requiring multi-post consistency ($\ge 5$ sampled original posts, $\ge 3$ exceeding `3x` follower reach), high tracer quality depth, follower growth velocity, and $K$-community SimClusters `InterestedIn` projection.
* **Acceptance Criteria:** Filterable by workspace community and follower bracket (`<2.5k`, `2.5k–10k`, `10k–25k`, `25k–50k`) with outlier post previews and 1-click **Add to Community** / **Dismiss**.

#### Story 4.2 — Track Who Is Gaining Respect From Peers
* **User Story:** As a user, I want to see which smaller accounts are starting to get replies, quotes, and follows from the established heavy hitters in my network graph, so that I can spot rising authority before follower counts reflect it.
* **Scope:** Scores **Peer Respect ($E_c$)** weighting inbound follows (`3.0x`), quotes (`2.0x`), and replies (`1.0x`) from tracked accounts by their **Heavy Hitter + SALSA Scout Score** and **Distinct Endorser Diversity**.
* **Acceptance Criteria:** Displays exact peer co-sign badges and clickable interaction links per candidate.

---

### System Guardrail: Automated Upstream `x-algorithm` Sync Watcher (`decisions/0004`)
* **Purpose:** Keep TweetTracker's tracer permanently aligned with `xai-org/x-algorithm` despite xAI's near-nightly sync commits.
* **Scope:**
  * Daily scheduled job + manual **Check Upstream Now** button comparing `xai-org/x-algorithm` `main` against `last_synced_sha` (`9106e9f` baseline) via GitHub API, watching `home-mixer/{params,scorers,filters}/`, `vm-ranker/`, `xai-value-model/`, `phoenix/`, `thunder/`, `simclusters/`, `user-cred-v2/`, and `docs/` (ignoring `ads/` and infra churn).
  * **Deterministic `param!(...)` Parser:** Extracts exact `before -> after` parameter/flag tables (`param.rs`, `vm-ranker/params.rs`, `weights.rs`) with **1-Click Stage into Backtester** (`algo_param_versions`).
  * **Auto-Created GitHub Issues:** Automatically creates deduplicated `algo-sync` issues on the configured GitHub repository (using `GITHUB_TOKEN`, with commit links, parsed parameter diff table, optional Grok/Gemini Rust diff impact summary, and affected TweetTracker file checklist) + fires SSE/Webhook `algo.upstream_drift_detected`.

---

## 3. Deferred to Build 2 (Post-Core Backlog)

* **Epic 5 — Own-Account Diagnosis & Pre-Post Draft Check (`decisions/0004`):**
  * *Story 5.1:* Own-Post Hour-1 (`0–60m`) Flight Recorder & OON Escape Monitor.
  * *Story 5.2:* Top 20% vs. Bottom 20% Own-Post Pattern Breakdown.
  * *Story 5.3:* Single & Head-to-Head A/B Pre-Post Draft Check (using Build 1's collected 48h empirical baselines + `LLMProvider` router).

---

## 4. Core Database Schema & Configurable Cost Guardrails

* **PostgreSQL Tables:** `workspaces` (`id`, `name`, `slug`, `my_x_handle` nullable, cadences, `last_discovery_sync_at`, spend caps, `monthly_unit_budget` default `100_000`, active providers), `communities` (`id`, `workspace_id`, `name`, `slug`, `color`, `x_list_id`), `tracked_accounts` (incl. `core_affinity_score`, `yield_score`, `reply_sample_count`, `avg_reply_views`, `attributed_follows_30d`), `account_community_memberships` (manual tags + NMF `KnownFor`/`InterestedIn` weights), `relationship_edges`, `posts` (incl. `acted_action_kind` and lightweight text embeddings for DPP crowding), `post_snapshots`, `post_interactions`, `own_follower_snapshots`, `own_reply_outcomes`, `algo_param_versions`, `breakout_predictions`, `alert_deliveries`, `discovered_candidates` (incl. `core_affinity_score`, `yield_score`, `pruned_at`), `peer_endorsements`, `upstream_algo_events` (`base_sha`, `head_sha`, `param_diffs_json`, `github_issue_url`), `api_credit_ledger`.
* **Reply-Yield Core Learning, 28d Seed Bootstrap Decay & Hysteresis Pruning (`gentoftech/tweettracker#1`):** Seeds kick off the initial crawl for **`28` days** (`SEED_BOOTSTRAP_GRACE_DAYS = 28`), not permanent core membership. Cold-start scoring uses SimClusters + `UserCredV2` affinity (`core_affinity_score` + audience/hot-post/`20x`-mutual reach prior); as `my_x_handle` replies/quotes accumulate via `GET /tw-v2/user/tweets-and-replies` + `OwnFollowerSnapshot` + `2h–48h` batch refresh (`~1–2` units/sync), Empirical Bayes `yield_score` blends observed average reply impressions and impression-weighted `[0h, 24h]` attributed follower gains (`6h` decay). Proven high-yield accounts enter `C_yield` (`core_ids`), drive crawl priority and 2-hop discovery, and rank `Hot Posts` by `expected_reply_reach_score`. Auto-promotes `>= 0.55` (`0.63` when throttled) and prunes `< 0.32` after `48h` (auto-adds) or `28d` (bootstrap seeds whose replies flop or never produce climbing Hot Posts, with `MIN_ACTIVE_CORE_FLOOR = 10` unattended safety floor), with no per-sync or total tracked-account cap (`pruned_at` settling memory ensures pruned accounts return only on new conversational/reply evidence).
* **Built-In Spend & Unit Budget Meter:** Logs exact units/credits consumed per provider call (`$0.00025`/unit parity on the `100,000`-unit/month `$25` TweetAPI plan, reconciled via `0`-unit `GET /tw-v2/account/usage`) against configurable daily/monthly USD caps and `monthly_unit_budget` (`throttled` discovery at `>=80%` projected 30d burn, `paused` crawl at `>=95%`).

---

## 5. Build 1 Sequence (Once Code Boundary Is Approved)

1. **Stage 1 — Scaffold, Docker Compose, Multi-Workspace Postgres Schema, Exact `xai-value-model`/`vm-ranker` Tracer Engine + `FixtureProvider` & Frontend Shell**
2. **Stage 2 — Live `TweetAPIProvider` Adapter + Spend Guardrail Ledger + Account/List Ingestion**
3. **Stage 3 — Epic 1 (Custom $K$-Communities, Seeded NMF Mini-SimClusters, Heavy Hitters w/ `UserCredV2` Mass, $N$-Pole Radial Bridge Map & Optional-Handle Mutual CRM)**
4. **Stage 4 — Epic 2 (Paced Radar Poller, Two-Stage Tracer + DPP Crowding + Multi-Signal SEISMIC, Reply-vs-Quote Guidance, Live SSE Feed & HMAC Webhooks)**
5. **Stage 5 — Epic 3 (48h Outcome Grader, Accuracy Scorecard, Holdout-Gated Tuner, Parameter Changelog & `<2s` Backtest Sandbox)**
6. **Stage 6 — Epic 4 (`<50k` Rising Voices + SALSA Peer Co-Signs) & Automated Upstream `Algo Sync Watcher` (Auto GitHub Issues)**
