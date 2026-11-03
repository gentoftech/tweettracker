# 0003 — Pluggable X Data Provider Router; Ship TweetAPI.com + FixtureProvider in Build 1

Status: accepted 2026-11-01

## Decision

Define a provider-agnostic `XDataProvider` interface in `worker`/`api` supporting capability-level routing (`tweetapi_com`, `socialdata`, `twitterapi_io`, `official_x`, `fixture`), and **ship `TweetAPIProvider` (`TWEETAPI_API_KEY`, `https://api.tweetapi.com/tw-v2`) + offline `FixtureProvider` active in Build 1** (Option A).

## What forced it

- Verified pricing math in `research/x-data-provider-pricing.md` shows Official X API v2 Pay-Per-Use (`$5.00`/1k post reads, `$10.00`/1k follow edges) costs `$413–$435/mo` at `150` accounts and `$1,375–$4,350/mo` at `500–1,500` accounts *even without* crawling peer `following` edges (`+$1,500–$15,000/mo`).
- **TweetAPI.com (`tweetapi.com`)** bills `1 API unit` per HTTP call (`$17/mo` Pro = `100k` units at `20 req/s`; `$57/mo` Ultra = `500k` units at `30 req/s`; `$5` PAYG = `10k` 365-day units auto-covering spillover). Because `/tw-v2/tweet/details-by-ids` rehydrates up to `200` post snapshots in `1` unit and `/tw-v2/user/following-list?count=200` fetches `200` follow profiles in `1` unit, a single API key covers `150` accounts for **`$17.00/mo`**, `500` accounts for **`~$20–$31/mo`**, and `1,500` accounts for **`$57.00/mo`** (or **`$17.00/mo` even at `1,500` accounts** when polling 3 niche X Lists via `list:<listId>`).
- Keeping `socialdata`, `twitterapi_io`, and `official_x` as typed config slots in `workspace_settings` preserves zero-refactor failover or hybrid routing later without complicating Build 1.

## Consequences

- **Build 1 adapters:** `TweetAPIProvider` (using char-budgeted `<=512`-char `from:` batching *or* `list:<listId>` polling, `200`-ID batch snapshot rehydration, `100`-handle batch profile lookup, `200`/page `following-list` sync, and free `0`-unit `GET /tw-v2/account/usage` ledger reconciliation) + `FixtureProvider` (zero-key local dev & CI tests).
- **Scheduler pacing:** `worker` staggers poll requests smoothly across each radar cycle so rate limits stay flat and future `SocialData` failover automatically stays within its `3` free empty polls/min rule.
- **Stubbed slots:** `SocialDataProvider`, `TwitterAPIioProvider`, and `OfficialXProvider` remain ready config slots in Build 1 until needed.
