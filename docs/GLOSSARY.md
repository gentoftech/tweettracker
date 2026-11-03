# TweetTracker — GLOSSARY

Terms as this project uses them. One sentence each.

## X Algorithm (`xai-org/x-algorithm`, Live `9106e9f` — Oct 31, 2026)

- **Home Mixer (`home-mixer/`)** — X's Rust orchestration pipeline that sources, hydrates, filters, scores, and blends candidates for the "For You" timeline.
- **Thunder (`thunder/`)** — X's sub-millisecond Rust in-memory store holding in-network posts from followed accounts with a strict 48-hour retention cutoff.
- **Phoenix (`phoenix/`)** — X's Grok-based two-stage ML engine: a Two-Tower model (`1,024`-action sequence) for Out-of-Network retrieval and a Candidate-Isolated Transformer for multi-action probability prediction.
- **Value Model & VM Ranker (`xai-value-model/`, `vm-ranker/`)** — X's final Rust scoring crates that fuse 29 Phoenix action/dwell heads (`scoring.rs`), apply `offset_score`, penalize OON and in-network replies/retweets (`0.75x`), apply author diversity decay, and run DPP semantic deduplication.
- **Bidirectional Reply Boost** — The `+15.0` bonus added to `ReplyWeight` (`5.0` → `20.0`) strictly on non-reply, non-retweet posts (`!is_reply && !is_retweet`, which includes Original Posts and Quote-Posts) when viewer and author mutually follow.
- **OON & Reply/Retweet Rescore (`0.75x`)** — `vm-ranker`'s `OonWeightFactor = 0.75` penalty applied both to unfollowed Out-of-Network candidates and (via `EnableOonRescoreForInNetworkRepliesRetweets = true`) to in-network replies and retweets.
- **OON Retweet/Reply Filter** — `home-mixer/filters/oon_retweet_reply_filter.rs`, which drops Out-of-Network retweets and replies (`EnablePhoenixOonReplies = false`), meaning only Original Posts and Quote-Posts travel Out-of-Network on their own.
- **Self-Reply Chain Filter** — `home-mixer/filters/self_reply_chain_filter.rs`, which keeps pure self-reply threads (`author → author`) for followers while dropping self-replies chained onto unfollowed third parties.
- **Author Diversity Multiplier** — `xai-value-model/scoring.rs`'s exact `(1 - 0.25) * 0.5^k + 0.25` decay (`1.0x`, `0.625x`, `0.4375x`..., `0.25x` floor) applied to multiple posts from the same author in a batch.
- **DPP Reranker (`dpp_model.rs`)** — `vm-ranker`'s Determinantal Point Process (`DppTheta = 0.65`, top `150` ranks) that suppresses semantically near-duplicate posts using post embeddings.
- **Cold-Start Window** — `author_cold_start.rs`'s Thompson Sampling (`Beta(0.75, 49.25)`) lifting authors with `<50,000` followers on posts aged `<= 2 hours` and `<200` impressions into slots `15–16`.
- **`UserCredV2` (`user-cred-v2/`)** — X's active 2026 PageRank-mass user credibility job (`score = clamp(165.2 + 7.07 * ln(mass), 0, 100)`) with daily VIT snapshot safeguards.

## Workspaces, Graph & Tracer Metrics (Epics 1–4 & Algo Sync)

- **Workspace** — An isolated setup inside TweetTracker holding its own optional `my_x_handle`, tracked accounts, optional X Lists (`x_list_ids_json`), thresholds, and webhooks (with zero manual Circle setup required).
- **Behavioral Graph** — Directed, exponentially time-decayed (`14-day` half-life) interaction and follow graph weighted by `xai-value-model` multipliers, replacing stale all-time follower crawls.
- **Auto-Detected Interaction Clusters** — Unsupervised Louvain modularity pockets detected automatically over the behavioral graph (`simclusters/`-style audience co-engagement) to color and cluster connected voices with zero manual tagging.
- **Heavy Hitter Score ($H_u$)** — A `0–100` network influence score combining 60-minute downstream catalysis lift, weighted $s$-core depth, SALSA Authority, and `UserCredV2`-style log-mass.
- **SALSA Scout (Hub)** — An account whose outbound follows, quotes, and replies consistently point early to rising Authorities, used to weight Story 4.2 Peer Respect.
- **Two-Stage Algo Tracer** — TweetTracker's external scoring engine combining Stage 1 Retrieval Eligibility/Velocity with an exact port of `xai-value-model/scoring.rs` + `vm-ranker` multipliers over Empirical-Bayes-shrunk action rates and calibrated latent-head surrogates.
- **Multi-Signal SEISMIC** — Self-exciting Hawkes cascade estimator computing `15–60m` breakout infectiousness across weighted actions and Heavy Hitter catalysts.
- **Algo Sync Watcher** — Daily `worker` job comparing `xai-org/x-algorithm` `HEAD` against `last_synced_sha`, parsing `param!(...)` weight/flag changes, and auto-creating `algo-sync` GitHub Issues on `gentoftech/tweettracker`.
- **Fixture Mode** — Deterministic offline dataset bundled in code for zero-cost local UI dev and automated tests when no live API key is active.
