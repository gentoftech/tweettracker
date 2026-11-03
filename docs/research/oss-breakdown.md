# Open-Source Repository & Dependency Breakdown

Related docs: [`../PRD.md`](../PRD.md) | [`how-x-algo-works-today.md`](how-x-algo-works-today.md) | [`single-and-multi-community-graphs.md`](single-and-multi-community-graphs.md)

---

## 1. Master Verdict Matrix (Audited by Date & License)

| Verdict | Repository / Package | Last Active / Created | License | Target Epic(s) |
| :--- | :--- | :--- | :---: | :--- |
| **Adopt Directly** | [`xai-org/x-algorithm`](https://github.com/xai-org/x-algorithm) *(constants & formulas)* | **Updated Oct 2026** | Apache-2.0 | All Epics (1–5) |
| **Adopt Directly** | [`xdevplatform/xdk-python`](https://github.com/xdevplatform/xdk-python) *(optional Official Owned-Reads slot)* | **Active 2024–2026** | MIT | Ingestion |
| **Adopt Directly** | [`networkx`](https://github.com/networkx/networkx) + [`GiulioRossetti/cdlib`](https://github.com/GiulioRossetti/cdlib) + `scikit-learn` / `scipy` | **Active 2025–2026** | BSD-3 / BSD-2 | Epics 1, 3, 4 |
| **Adopt Directly** | [`graphology`](https://github.com/graphology/graphology) + [`sim51/react-sigma`](https://github.com/sim51/react-sigma) (`Sigma.js`) | **Active 2025–2026** | MIT | Epic 1 Network UI |
| **Adapt Pattern** | [`sheeki03/x-algo-tweet-scorer`](https://github.com/sheeki03/x-algo-tweet-scorer) | **Created May 2026** | MIT | Epic 5 (Story 5.3) |
| **Adapt Pattern** | [`attainmentlabs/x-open-source-algorithm-skill`](https://github.com/attainmentlabs/x-open-source-algorithm-skill) | **2026** | MIT | Epic 5 (Story 5.3) |
| **Adapt Pattern** | [`TheExGenesis/community-archive`](https://github.com/TheExGenesis/community-archive) | **Active 2024–2026** | MIT | DB Schema (Epics 1–4) |
| **Reference Math Only** | [`twitter/the-algorithm`](https://github.com/twitter/the-algorithm) (`simclusters_v2`, `real_graph`) | **March 2023** *(Frozen)* | Apache-2.0 | Epic 1 & 4 Graph Math |
| **Reference Math Only** | [`cran/seismic`](https://github.com/cran/seismic) *(Stanford SNAP Hawkes cascade)* | **2015** | GPL-2 | Epic 2 Cascade Velocity |
| **Reject (Do Not Use)** | [`eleurent/twitter-graph`](https://github.com/eleurent/twitter-graph) | **2020–2022** | **None (Proprietary)** | *Unlicensed + legacy full-follower crawl* |
| **Reject (Do Not Use)** | `leidenalg` *(Python C-extension)* | **Active** | **GPL-3.0** | *Copyleft GPL; BSD `networkx`/`cdlib` covers scope* |

---

## 2. Detailed Breakdown by Functional Area

### 2.1 Official X Recommendation Codebases
* **[`xai-org/x-algorithm`](https://github.com/xai-org/x-algorithm) (Apache-2.0 — Updated Oct 2026, ~34k ⭐):** Source of truth for `home-mixer/params/param.rs` (Aug 14 & Oct 2026 drops), `ranking_scorer.rs`, `oon_scorer.rs` (`0.75`), `author_diversity_scorer.rs` (`0.5` decay, `0.25` floor), `author_cold_start.rs` (`<50k` followers, `7,200s`, `200` impressions), `oon_retweet_reply_filter.rs`, `age_filter.rs` (`48h`), and `docs/BIDIRECTIONAL_BOOST_CHANGE.md` (`+15.0` mutual reply boost).
* **[`twitter/the-algorithm`](https://github.com/twitter/the-algorithm) (Apache-2.0 — March 2023):** Legacy scoring weights, `TweepCred`, and `HeavyRanker` are obsolete in 2026. Referenced strictly for the conceptual math of `simclusters_v2` (`KnownFor` / `InterestedIn`) and `real_graph` (exponentially time-decayed tie strength).

### 2.2 X Ingestion Clients
* **`httpx` (BSD-3) Reseller Adapters (`TwitterAPI.io` / `SocialData.tools`) + [`xdevplatform/xdk-python`](https://github.com/xdevplatform/xdk-python) (MIT):** Clean async REST adapters wrapped behind a unified `XDataProvider` protocol with real-time credit metering and daily/monthly spend caps.

### 2.3 Pre-Post Draft Scorers & Simulators (Epic 5)
* **[`sheeki03/x-algo-tweet-scorer`](https://github.com/sheeki03/x-algo-tweet-scorer) (MIT — Created May 2026):** Strong Next.js dual-engine UX pattern (deterministic structural rules + LLM content judge for 280-char and long-form posts). Because it was published in May 2026—predating the July 24 bidirectional boost update, August 14 `param.rs` weight reveal, and October 8 full-read dwell update—TweetTracker adapts its UI/evaluator pattern while replacing its static weights with verified Oct 2026 `param.rs` rules and empirical Story 5.2 own-account history.
* **[`attainmentlabs/x-open-source-algorithm-skill`](https://github.com/attainmentlabs/x-open-source-algorithm-skill) (MIT — 2026):** Reference rubric decomposition across Phoenix's predicted signals (`follow_author`, `share_via_copy_link`, `reply`, `quote`, `dwell` vs. `favorite`).

### 2.4 Graph Storage, Community Math & WebGL Rendering (Epics 1, 3 & 4)
* **[`TheExGenesis/community-archive`](https://github.com/TheExGenesis/community-archive) (MIT — Active 2024–2026):** Relational PostgreSQL indexing patterns for tweet trees, quote/reply edges, and account snapshots.
* **`networkx` (BSD-3) + [`GiulioRossetti/cdlib`](https://github.com/GiulioRossetti/cdlib) (BSD-2) + `scipy` / `scikit-learn` (BSD-3):** Provides weighted $s$-core, `nx.constraint` (Burt's Structural Holes), `nx.louvain_communities`, `sklearn.decomposition.NMF` (Seeded Mini-SimClusters), `cdlib.algorithms.slpa`, and $L_2$-regularized logistic calibration with zero GPL dependencies.
* **[`graphology`](https://github.com/graphology/graphology) + [`@react-sigma/core`](https://github.com/sim51/react-sigma) (MIT):** 60fps WebGL rendering + WebWorker ForceAtlas2 physics for `500–5,000` nodes and `20,000+` edges.

### 2.5 Early Breakout Cascade Prediction (Epic 2)
* **[`cran/seismic`](https://github.com/cran/seismic) (GPL-2 — KDD 2015 R Mirror):** Not bundled (R + GPL-2 + 2015 retweet-only assumptions). We implement the closed-form Hawkes kernel infectiousness equation from Zhao et al. (*KDD 2015*) cleanly in ~80 lines of `numpy`/`scipy`, parameterized with Oct 2026 `param.rs` action weights and Heavy Hitter scores.
