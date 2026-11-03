# Developing Single- & Multi-Community Graphs on X

Related docs: [`../PRD.md`](../PRD.md) | [`how-x-algo-works-today.md`](how-x-algo-works-today.md) | [`x-data-provider-pricing.md`](x-data-provider-pricing.md)

---

## 1. Base Layer: Behavioral Time-Decayed Graph (Replacing Static Follower Lists)

Static all-time follower graphs include millions of dormant accounts that never enter Phoenix's rolling `1,024`-action sequence. For each **Workspace**, TweetTracker builds a **directed, exponentially time-decayed behavioral + follow graph** $G = (V, E, w)$ parameterized with `xai-value-model` weights and a `14-day` half-life:

$$w_{u \to v}(T) = \alpha_{\text{follow}}\mathbb{I}[u \to v] + \alpha_{\text{mutual}}\mathbb{I}[u \leftrightarrow v] + \sum_{e \in \mathcal{E}_{u \to v}} W_{\text{type}(e)} \cdot 2^{-(T - t_e) / 14\text{d}}$$

* **Inputs:** (1) Optional workspace `my_x_handle` follower/following ties (for Story 1.4 & `20x` mutual badges), (2) peer-to-peer `following` edges across curated accounts (`200`/call on `TweetAPI.com`), and (3) rolling reply, quote, and repost edges captured during post monitoring ($W_{\text{mutual\_reply}} = 20.0$, $W_{\text{reply}} = 5.0$, $W_{\text{quote}} = 5.0$, $W_{\text{repost}} = 0.75$).

---

## 2. Single-Community Graphs (Mapping *Within* Any Workspace Community $c$)

When analyzing one user-defined community in isolation (induced subgraph $G_c$), four algorithms separate real movers from high-follower ghosts:

1. **Sub-Pocket Discovery — Weighted Louvain Modularity (`networkx.community.louvain_communities`):** Partitions a single community into tight sub-topics automatically without manual sub-lists.
2. **Core Superspreaders — Weighted $s$-Core Decomposition (*Kitsak et al., Nature Physics 2010*):** Identifies accounts embedded in the dense inner conversational core rather than high-follower periphery, aligning with X's `BidirectionalFollowReplyWeightBoost = +15.0` (`20.0` mutual reply weight).
3. **Authorities, Scouts & `UserCredV2` Mass (*Gupta et al., WWW 2013* + `user-cred-v2/UserCredV2.scala`):**
   * **SALSA Authority ($a_u$) & `UserCredV2` Log-Mass:** Scales network mass via X's active 2026 curve `clamp(165.2 + 7.07 * ln(mass), 0, 100)` → feeds **Story 1.2 Heavy Hitters**.
   * **SALSA Hub / Scout ($h_u$):** Curators whose outbound follows, quotes, and replies consistently point early to rising Authorities → powers **Story 4.2 Peer Respect** in Epic 4.
4. **Empirical Downstream Catalysis Lift ($L_u$):** Observed excess 60-minute velocity ratio $\frac{\Delta I(t_0 \to t_0+60\text{m})}{\text{Expected }\Delta I}$ on tracked posts after account $u$ replies or quotes them.

---

## 3. Dynamic $K$-Community Overlapping Graphs ($K \ge 2$ User-Defined Communities)

Hard-partition clustering (Louvain, K-Means) forces every account into a single bucket—erasing Bridge accounts. Because `xai-org/x-algorithm` still actively runs `SimClusters v2` (`EnableSimclustersSource = true`, `48h` max age), TweetTracker generalizes overlapping community math to **any arbitrary number $K \ge 2$ of user-defined workspace communities**:

### 3.1 Behavioral Mini-SimClusters via $K$-Component Seeded NMF (Adapting *Satuluri et al., KDD 2020*)
1. **Bipartite Engager $\times$ Producer Matrix ($M \in \mathbb{R}_+^{N \times P}$):** Rows are active engagers/followers; columns are tracked/candidate creators ($M_{u,p} = w_{u \to p}$).
2. **Producer–Producer Co-Engagement Similarity ($S_{ij}$):** Cosine similarity between column vectors of $M$—high when the same audience engages with both creators.
3. **Seeded Non-Negative Matrix Factorization ($K$ Communities):** Anchored by Story 1.1 community tags $V_0 \in \mathbb{R}_+^{P \times K}$:
   $$\min_{W \ge 0, V \ge 0} \| M - W V^\top \|_F^2 + \lambda_{\text{seed}} \| P_{\Omega}(V - V_0) \|_F^2 + \lambda_1 \|V\|_1$$
   * **`KnownFor` ($V \in \mathbb{R}_+^{P \times K}$):** Soft multi-community anchor strength $(v_{u,1}, \dots, v_{u,K})$ for every tracked creator—revealing cross-community reach even if manually tagged in only one list.
   * **`InterestedIn` ($U = M \cdot V$):** Projects any newly discovered `<50k` Rising Voice (Epic 4) into the workspace's $K$ communities via a single matrix multiply.
   * **Emergent Micro-Bridges:** Optional **SLPA** (`cdlib.algorithms.slpa`) detects unseeded overlap pockets.

### 3.2 Pinpointing True Bridge Accounts Across $K$ Communities (Story 1.3)
Combines two orthogonal metrics generalized for any $K \ge 2$:
1. **$K$-Normalized Guimerà–Amaral Participation Coefficient ($\tilde{P}_u$, *Nature 2005*):**
   $$\tilde{P}_u = \frac{K}{K - 1} \left( 1 - \sum_{c=1}^{K} \left(\frac{w_{u,c}}{w_u}\right)^2 \right) \in [0, 1]$$
   Normalizing by $\frac{K}{K-1}$ keeps thresholds invariant across `2`, `3`, `5`, or `10` communities, cleanly separating **Provincial Heavy Hitters** ($\tilde{P}_u < 0.40$) from **2-Way Bridges** and **Multi-Circle Core Connectors** ($\tilde{P}_u > 0.70$).
2. **Burt's Structural Holes / Network Constraint ($C_u$, *AJS 2004* — `networkx.constraint`):** Low constraint ($1 - C_u$ high) identifies brokers connecting *otherwise disconnected* community sub-clusters so a Quote-Post jumps into net-new audience pools.

---

## 4. Visualization & Compute Performance

* **Single-Community View ($K \ge 1$):** WebGL ForceAtlas2 (`Sigma.js` + `graphology`), sizing nodes by Heavy Hitter Score (toggleable vs. Raw Followers).
* **$N$-Pole Radial Bridge Map ($K \ge 2$):** Places $K$ community anchor poles at $\mathbf{p}_c = (\cos \frac{2\pi(c-1)}{K}, \sin \frac{2\pi(c-1)}{K})$ on the unit circle ($K=2$ axis, $K=3$ equilateral triangle, $K=4$ square, $K=5+$ polygon) and positions every creator by convex affinity $\mathbf{x}_u = \sum_{c=1}^{K} \hat{v}_{u,c}\,\mathbf{p}_c$ (with mild collision jitter), placing pure community voices at the poles and 2-way/multi-way Bridges along connecting chords and the center.
* **Runtime:** Executes in `<1.5s` on a single CPU core in `scipy.sparse` / `networkx` for up to `5,000` nodes and `100,000` edges per workspace.
