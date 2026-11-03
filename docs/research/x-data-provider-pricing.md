# X Data Provider Pricing & Workload Audit (Late 2026)

Related docs: [`../PRD.md`](../PRD.md) | [`../decisions/0003-x-data-provider-architecture.md`](../decisions/0003-x-data-provider-architecture.md)

---

## 1. Clearing Up "Replay Sim" vs. Live Providers vs. Backtester

| Component | What It Actually Is | Costs API Credits? | Relates to Resellers? |
| :--- | :--- | :---: | :---: |
| **1. Live `XDataProvider`** *(TweetAPI.com, TwitterAPI.io, SocialData, Official X, etc.)* | Fetches real-time posts, impressions, bookmarks, replies, quotes, and follow edges from X in production. | **Yes** | **Yes — this is the vendor choice below.** |
| **2. Offline `FixtureProvider`** *(earlier called "Replay Sim")* | Canned JSON responses bundled in the repo so unit tests, CI, and UI dev work offline before API keys are configured. | **No (`$0`)** | **No** — purely a local dev/test mock. |
| **3. Epic 3 Historical Backtester** | Replays *your real collected post snapshots* already saved in PostgreSQL (`15m` → `48h`) to test new breakout thresholds/weights. | **No (`$0`)** | **No** — queries local Postgres only. |

---

## 2. Verified Late-2026 Unit Rate Card (`pricing.json` / `pricing.md`)

**Critical Paradigm Split:** **TweetAPI.com** bills **per HTTP call (`1 API unit/request`)** regardless of page size (up to `200` tweet IDs or `200` follow profiles in 1 call), whereas **TwitterAPI.io**, **SocialData.tools**, **TwexAPI**, and **Official X API v2** bill **per returned item**.

| Metric / Rule | Official X API v2 *(Pay-Per-Use)* | **TweetAPI.com** *(`tweetapi.com` — Per-Call)* | **TwitterAPI.io** *(`twitterapi.io` — Per-Item)* | **SocialData.tools** *(Per-Item)* | **TwexAPI** *(`twexapi.io` — Per-Item)* | **Apify X Actors** *(`APIFY_API_TOKEN` live)* |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| **Billing Basis** | Per item (24h UTC deduped) | **`1 API unit / call`** *(page/batch)* | Per item + `15`-credit call floor | Per item (`>3` empty/min billed) | Per item + `5`-credit empty call | Compute + per-item actor fee |
| **Plans / Wallet Structure** | PAYG (`2M–3M` cap) | PAYG `$5/10k` (`$0.50/1k` calls, 365d); **Pro `$17/mo` (`100k`)**; **Ultra `$57/mo` (`500k`)** | PAYG (`$1=100k` cr, no expiry); Subs `$29–$1,499/mo` (`1.13x–1.90x` cr) | Pure PAYG balance (no expiry, no min) | `$100` Standard (`10M` cr, no expiry); Subs from `$50/mo` *(⚠️ `$20` Mini = **1-day** expiry!)* | `$0` PAYG + actor rates |
| **Search / Replies / Quotes** *(effective per 1,000 posts)* | **`$5.00`** | **`~$0.0085 – $0.025`** *(`~15–20` posts/call)* | **`$0.15`** (`15` cr/tweet) | **`$0.20`** (`$0.0002`/item) | **`$0.05`** (`5` cr/item) | `$0.13 – $0.40` |
| **Batch Post Snapshots** *(per 1,000 posts by ID)* | **`$5.00`** *(same-UTC-day `$0`)* | **`~$0.00085 – $0.0025`** *(`200` IDs = `1` unit!)* | **`$0.15`** *(billed per ID requested)* | **`$0.20`** (`100` IDs/batch) | **`$0.05`** | `$0.13 – $0.40` |
| **Bulk User Profiles** *(per 1,000 users)* | **`$10.00`** | **`~$0.0017 – $0.005`** *(`100` handles = `1` unit)* | **`$0.01`** (`100+` batch) / `$0.18` (`<100`) | **`$0.20`** | **`$0.05`** | `$0.15 – $0.40` |
| **Following / Followers** *(per 1,000 edges)* | **`$10.00`** | **`~$0.00085 – $0.0025`** *(`200`/pg `following-list`)* | **`$0.01`** (`200`/pg) / `$0.0045` (IDs) | **`$0.20`** | **`$0.05`** | `$0.15 – $0.40` |
| **Empty Search Poll Cost** *(`since_id` returns `0`)* | **`$0.00`** | **`1 unit`** (`$0.00011–$0.00050`) | **`$0.00015`** (`15`-cr min; **not** added to non-empty calls) | **`$0.00` for first `3` empty/min**; **`$0.0002` after `3`/min!** | **`$0.00005`** (`5` cr/empty call) | `~$0.002+` container spin-up |
| **Rate Limits** | Tier-dependent | `20 req/s` (PAYG/Pro) – `60 req/s` | `20 – 500+ req/s` | `120 req/min` (free bump on req) | `5 – 30 req/s` | Slow (`10–30s` cold start) |

---

## 3. Workload Assumptions & Four Math Corrections Found in Audit

Modeled across `150`, `500`, and `1,500` curated accounts assuming `2.0` original/quote posts/account/day, `10-minute` radar polling (`4,320` cycles/month), `5.0` lifecycle snapshots/post (`15m..48h`), `400` reply/quote + Epic 4 items/account/month, and `1,000` peer `following` edges/account/month:

| Monthly Workload | 150 Accounts | 500 Accounts | 1,500 Accounts |
| :--- | :---: | :---: | :---: |
| **A. Radar Search Polls** (`25` handles/query baseline; `~70.7%` empty via Poisson $\lambda=0.35$) | `25,920` reqs (`~18.3k` empty, `9k` posts) | `86,400` reqs (`~61.1k` empty, `30k` posts) | `259,200` reqs (`~183.2k` empty, `90k` posts) |
| **B. Lifecycle Snapshots** (`15m..48h`, `5.0` raw reads/post; `1.5–2.0` *incremental* UTC days on Official) | `45k` raw (`~10.4` IDs/cycle) | `150k` raw (`~34.7` IDs/cycle) | `450k` raw (`~104.2` IDs/cycle) |
| **C. Reply/Quote Threads + Epic 4 Rising Voice Sampling** | `60k` items (`~4k` pages) | `200k` items (`~13.3k` pages) | `600k` items (`~40k` pages) |
| **D. Monthly Peer `following` Graph Sync** (~`1,000` follows/account) | `150k` edges (`750` pages) | `500k` edges (`2,500` pages) | `1,500k` edges (`7,500` pages) |

### Key Math Corrections vs. Earlier Draft
1. **Added `TweetAPI.com` (Per-Call Powerhouse):** Because `/tw-v2/tweet/details-by-ids` rehydrates up to **`200` post IDs for `1 API unit`**, all `45k–450k` monthly snapshots fit in **`4,320` calls/month** (`<$2.10/mo` even at `1,500` accounts!). Total `TweetAPI.com` volume is `~35k` units (`150` accts → **`$17/mo` Pro**), `~107k` units (`500` accts → **`$20.28/mo` Pro + PAYG**), and `~311k` units (`1,500` accts → **`$57/mo` Ultra**).
2. **Fixed `TwitterAPI.io` Empty-Poll Double-Count:** Earlier math charged `15` credits on *every* request **plus** `15` credits/tweet (`$5.24 / $17.46 / $52.38`). Per `twitterapi.io/pricing.json`, the rule is `max(15, 15 * N_tweets)`—only the `~70.7%` *empty* polls pay the minimum, lowering Workload A to **`$4.10 / $13.66 / $40.97`**.
3. **Caught `SocialData.tools`'s ">3 Empty Requests/Minute" Clause:** Per `socialdata.tools/pricing.md`, empty calls are free **only for the first 3 empty requests per minute** (`$0.0002` each thereafter). At `150–500` accounts (`0.6–2.0` queries/min), empty polls stay `$0` **only if the worker paces/jitters queries across the 10m window** (unpaced cron bursts add `+$1–$10/mo`). At `1,500` accounts (`6.0` queries/min), even perfect pacing exceeds `3` empty/min, raising SocialData Workload A from `$18.00` to **`$29.37/mo`** (`$52.04` unpaced).
4. **Caught `TwexAPI` Plan Expiry & Cash Floor:** `TwexAPI`'s `$20 Mini` pack (`2M` credits) **expires in 1 day**, and subscriptions start at **`$50/mo` (`Basic`)**. Achieving the `$14.12 / $47.05` effective burn requires buying the **`$100 Standard` one-time pack (`10M` non-expiring credits)** and drawing it down over multiple months.
5. **512-Char Query Limit & `list:<id>` Optimization:** Packing `25` handles into `(from:h1 OR ...) -filter:replies since_id:...` overflows X's `512`-character limit if handles average `>10` chars (`20` handles/query is safe, adding `+25%` search calls). Better yet: syncing our 3 circles into **3 X Lists** and querying `list:<listId> -filter:replies since_id:...` cuts search calls to **`12,960/mo` at any scale**—fitting even **`1,500` accounts inside `TweetAPI.com`'s `$17/mo` Pro tier (`~66k` total units/mo)**!

---

## 4. Verified Monthly Cost Comparison by Scale

*(Note: Epic 1 daily batch profile refreshes + Epic 5 `@gentoftech` 5m flight-recorder polls add `<$1/mo` on `TweetAPI.com`/`TwitterAPI.io` and `$1–$9/mo` on `SocialData`; toggleable in the interactive calculator.)*

### Scenario 1: **150 Curated Accounts** (Tight Core List)

| Provider Strategy | A. Radar Search | B. Snapshots | C. Replies & Epic 4 | D. Peer Follow Graph | **Monthly Total** |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **Hybrid: SocialData (Search) + TweetAPI.com (PAYG B–D)** | `$1.80` *(paced)* | `$2.16` | `$2.00` | `$0.38` | **`$6.33 / mo`** *(amortized PAYG)* |
| **TwexAPI.io** *(`$100` non-expiring wallet; `$50/mo` if sub)* | `$1.37` | `$2.25` | `$3.00` | `$7.50` | **`$14.12 / mo`** *(`$50/mo` sub floor)* |
| **TweetAPI.com (Single Key — Pro `$17/mo`, 35% used)** | `$12.59` | `$2.10` | `$1.94` | `$0.36` | **`$17.00 / mo`** *(`$11.32` w/ X Lists)* |
| **Hybrid: SocialData (Search) + TwitterAPI.io (B–D)** | `$1.80` | `$6.75` | `$9.00` | `$1.50` | **`$19.05 / mo`** |
| **TwitterAPI.io (Single Key — PAYG)** | `$4.10` | `$6.75` | `$9.00` | `$1.50` | **`$21.35 / mo`** |
| **SocialData.tools (Single Key — Paced PAYG)** | `$1.80` | `$9.00` | `$12.00` | `$30.00` | **`$52.80 / mo`** |
| **Official X API v2 Only** *(no peer follow crawl)* | `$45.00` | `$67.50–$90` *(UTC dedup)* | `$300.00` | *Skipped (`+$1,500`!)* | **`$412.50 – $435 / mo`** |

### Scenario 2: **500 Curated Accounts** (Full 3-Niche Map)

| Provider Strategy | A. Radar Search | B. Snapshots | C. Replies & Epic 4 | D. Peer Follow Graph | **Monthly Total** |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **Hybrid: SocialData (Search) + TweetAPI.com (PAYG B–D)** | `$6.00` *(paced)* | `$2.16` | `$6.67` | `$1.25` | **`$16.08 / mo`** |
| **TweetAPI.com (Single Key — Pro `$17` + PAYG spillover)** | `$16.44` | **`$0.82`** | `$2.54` | **`$0.48`** | **`$20.28 / mo`** *(`$31.10` @ 20/q; `$17` w/ Lists)* |
| **TwexAPI.io** *(`$100` non-expiring wallet; `$50/mo` if sub)* | `$4.55` | `$7.50` | `$10.00` | `$25.00` | **`$47.05 / mo`** *(`$50/mo` sub floor)* |
| **Hybrid: SocialData (Search) + TwitterAPI.io (B–D)** | `$6.00` | `$22.50` | `$30.00` | `$5.00` | **`$63.50 / mo`** |
| **TwitterAPI.io (Single Key — PAYG)** | `$13.66` | `$22.50` | `$30.00` | `$5.00` | **`$71.16 / mo`** |
| **SocialData.tools (Single Key — Paced PAYG)** | `$6.00` | `$30.00` | `$40.00` | `$100.00` | **`$176.00 / mo`** |
| **Official X API v2 Only** *(no peer follow crawl)* | `$150.00` | `$225–$300` | `$1,000.00` | *Skipped (`+$5,000`!)* | **`$1,375 – $1,450 / mo`** |

### Scenario 3: **1,500 Curated Accounts** (Broad Ecosystem Scale)

| Provider Strategy | A. Radar Search | B. Snapshots | C. Replies & Epic 4 | D. Peer Follow Graph | **Monthly Total** |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **Hybrid: SocialData (Search) + TweetAPI.com (Pro `$17`)** | `$29.37` *(>3/min rule)* | `$1.42` | `$13.12` | `$2.46` | **`$46.37 / mo`** |
| **TweetAPI.com (Single Key — Ultra `$57/mo`, 62% used)** | `$47.50` | **`$0.79`** | `$7.33` | **`$1.37`** | **`$57.00 / mo`** *(`$17.00` w/ 3 X Lists!)* |
| **TwexAPI.io** *(`$100` Standard / `$499` Scale wallet)* | `$13.66` | `$22.50` | `$30.00` | `$75.00` | **`$141.16 / mo`** *(`$117.40` on Scale)* |
| **Hybrid: SocialData (Search) + TwitterAPI.io (B–D)** | `$29.37` *(>3/min rule)* | `$67.50` | `$90.00` | `$15.00` | **`$201.87 / mo`** |
| **TwitterAPI.io (Single Key — PAYG / Pro `$199` Sub)** | `$40.97` | `$67.50` | `$90.00` | `$15.00` | **`$213.47 / mo`** *(`~$199` on Pro `1.41x`)* |
| **SocialData.tools (Single Key — Paced PAYG)** | `$29.37` | `$90.00` | `$120.00` | `$300.00` | **`$539.37 / mo`** |
| **Official X API v2 Only** *(no peer follow crawl)* | `$450.00` | `$675–$900` | `$3,000.00` | *Skipped (`+$15,000`!)* | **`$4,125 – $4,350 / mo`** |

---

## 5. Bottom-Line Architecture Takeaways

1. **Single-Key Value Leader — `TweetAPI.com` (`$17–$57/mo` across `150–1,500` accounts):** Paying per HTTP page/batch rather than per item collapses snapshot rehydration (`200` IDs/call), thread/quote sampling (`~15–20`/call), and `following-list` syncs (`200`/call) into a tiny fraction of per-item pricing—and pairing it with 3 X Lists (`list:<id>`) keeps even `1,500` accounts inside the **`$17/mo` Pro tier**.
2. **Best Failover / Pure-PAYG Companion — `TwitterAPI.io` (`$21–$213/mo`) or `SocialData.tools`:** Both hold non-expiring PAYG credit balances with `$0` monthly commitment. Pairing **SocialData** (paced search) + **TweetAPI.com** (batch snapshots/threads/graph) hits the theoretical floor (`$6.33/mo` at `150` accts, `$16.08/mo` at `500` accts) while giving automatic dual-vendor failover.
3. **Worker Must Pace Cron Polls:** Staggering search queries evenly across each 10-minute window keeps empty calls under SocialData's `3/min` free threshold (up to ~`750` accounts) and smooths rate-limit spikes across all providers.
4. **Apify's Day-1 Role (`<$0.10` one-time):** If `APIFY_API_TOKEN` is already configured, it remains handy for a one-time historical backfill of an account's past 200 posts, though `TweetAPI.com` (`10` calls = `$0.005`) or `TwitterAPI.io` (`$0.03`) can also do this natively in `<2 seconds`.
