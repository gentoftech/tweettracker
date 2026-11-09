"""Reply reach: what an early reply from an account like the owner's gets under an account's posts.

Measured from everyone's replies, not just the owner's, so every tracked account and candidate
gets a score whether or not the owner has replied there (no feedback loop on the owner's reply
cadence). Fair-sampling rules (see the reply-reach issue):

- Only replies posted within `EARLY_WINDOW_MINUTES` of the parent post (late replies are buried).
- Compared like for like: repliers in a follower band around the owner's, matched on Premium.
- Views read once the reply is 24h+ old; reported as a median plus the share clearing 500 views.
- Platform accounts, the post's author and the owner are excluded.

Tracked accounts are sampled in two phases: their replies are collected while the parent post is
still young (60-150 min, so early replies are on the first reply pages), and views are read in a
batch 24h later. Candidates (untracked) are tested on up to three 1-4 day old posts, paging back
to their early replies.

Most threads carry only a handful of early replies, so the follower band is 10x either side of
the owner's followers and accounts are re-sampled daily to reach `MIN_MATCHED_SAMPLES` within
the 30-day window.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
import math
from statistics import median
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tweettracker.algo import PipelineParams
from tweettracker.models import (
    DiscoveredCandidate,
    PeerEndorsement,
    Post,
    ReplyReachSample,
    TrackedAccount,
    Workspace,
)
from tweettracker.providers import (
    ProviderNotConfiguredError,
    ReplyRecord,
    SpendCapExceededError,
    get_x_data_provider,
)

logger = logging.getLogger("tweettracker.reply_reach")

EARLY_WINDOW_MINUTES = 60.0
MATURE_AFTER = timedelta(hours=24)
SAMPLE_WINDOW_DAYS = 30
MIN_MATCHED_SAMPLES = 5
VIEWS_CLEAR = 500
FOLLOWER_BAND_FACTOR = 10.0
DEFAULT_FOLLOWER_BAND = (1_000, 50_000)
OWNER_PROFILE_REFRESH = timedelta(hours=24)

TRACKED_RESAMPLE_AFTER = timedelta(days=1)
TRACKED_POST_AGE_MIN = (60.0, 150.0)
TRACKED_POSTS_PER_RUN = {"normal": 12, "throttled": 4, "paused": 0}
PAGES_PER_TRACKED_POST = 3

CANDIDATE_RETEST_AFTER = timedelta(days=3)
CANDIDATE_POST_AGE = (timedelta(hours=24), timedelta(hours=96))
CANDIDATES_PER_SYNC = {"normal": 8, "throttled": 3, "paused": 0}
CANDIDATE_POSTS_PER_TEST = 3
PAGES_PER_CANDIDATE_POST = 3
# Prefer threads big enough to hold several early replies but small enough to read in a page or two
CANDIDATE_POST_MAX_REPLIES = 80

MIN_PARENT_REPLIES = 3
REFRESH_BATCH = 200
REFRESH_BATCHES_PER_RUN = 5

THREAD_REPLIER_MIN_VIEWS = VIEWS_CLEAR
THREAD_REPLIERS_PER_RUN = 25

# X's own platform accounts skew any sample (e.g. a brand account reply with 10M views)
PLATFORM_HANDLES = frozenset(
    {"x", "xbusiness", "xdevelopers", "xdata", "premium", "support", "verified", "xcreators",
     "safety", "xnews", "xai", "grok", "xengineering", "xenterprise"}
)

LEDGER_REFRESH = "/tw-v2/tweet/details-by-ids#reply-reach"
LEDGER_CANDIDATE_TIMELINE = "/tw-v2/user/tweets-and-replies#reply-reach"
REPLY_REACH_LEDGER_ENDPOINTS = ("/tw-v2/tweet/details", LEDGER_REFRESH, LEDGER_CANDIDATE_TIMELINE)

COLD_START_FOLLOWER_CAP: int = PipelineParams().cold_start_follower_cap


def _utc(dt: datetime | None, fallback: datetime | None = None) -> datetime:
    if dt is None:
        return fallback or datetime.now(timezone.utc)
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _norm(raw: str | None) -> str:
    return (raw or "").strip().lstrip("@").lower()


def reply_reach_score(median_views: float | None, share_500: float | None) -> float | None:
    """[0, 1]: half from median views on a log scale (10 -> 0, 100 -> 0.33, 1k -> 0.67, 10k -> 1),
    half from the share of matched early replies clearing 500 views."""
    if median_views is None or share_500 is None:
        return None
    med_norm = min(1.0, max(0.0, math.log10(max(float(median_views), 1.0) / 10.0) / 3.0))
    return round(0.5 * med_norm + 0.5 * min(1.0, max(0.0, float(share_500))), 4)


DEFAULT_ADD_THRESHOLD = 0.30
# Fixed prune floor: early replies there get under ~50 views and almost none clear 500.
# A floor relative to the tracked network would ratchet (each prune raises the next cut-off).
REACH_PRUNE_FLOOR = 0.12
MIN_MEASURED_FOR_RELATIVE = 10
MIN_ADD_PRUNE_GAP = 0.1
THROTTLED_ADD_MARGIN = 0.05


def reach_thresholds(tracked_scores: list[float]) -> tuple[float, float]:
    """(add, prune) reply-reach thresholds. A candidate must beat the measured network's median
    to get in (a fixed default until enough accounts are measured), so the bar rises as the
    network improves. An account falls out below the fixed floor, which always sits at least
    `MIN_ADD_PRUNE_GAP` under the add bar (hysteresis)."""
    scores = sorted(float(s) for s in tracked_scores)
    add = (
        scores[len(scores) // 2]
        if len(scores) >= MIN_MEASURED_FOR_RELATIVE
        else DEFAULT_ADD_THRESHOLD
    )
    add = max(add, REACH_PRUNE_FLOOR + MIN_ADD_PRUNE_GAP)
    prune = min(REACH_PRUNE_FLOOR, add - MIN_ADD_PRUNE_GAP)
    return round(add, 4), round(max(0.0, prune), 4)


def is_measured(row: TrackedAccount | DiscoveredCandidate) -> bool:
    return row.reply_reach_score is not None and int(row.reply_reach_samples or 0) >= MIN_MATCHED_SAMPLES


def follower_band(ws: Workspace) -> tuple[int, int]:
    owner = int(ws.owner_followers_count or 0)
    if owner <= 0:
        return DEFAULT_FOLLOWER_BAND
    return (max(1, int(owner / FOLLOWER_BAND_FACTOR)), int(owner * FOLLOWER_BAND_FACTOR))


def estimate_from_samples(
    samples: list[ReplyReachSample], band: tuple[int, int], owner_premium: bool | None
) -> tuple[float | None, float | None, int]:
    """(median views, share >= 500, matched count). Matches follower band + Premium; if fewer than
    `MIN_MATCHED_SAMPLES`, relaxes Premium; if still too few, returns no estimate."""
    in_band = [
        s for s in samples
        if s.matured and band[0] <= int(s.replier_followers or 0) <= band[1]
    ]
    matched = (
        [s for s in in_band if bool(s.replier_premium) == bool(owner_premium)]
        if owner_premium is not None
        else in_band
    )
    if len(matched) < MIN_MATCHED_SAMPLES:
        matched = in_band
    if len(matched) < MIN_MATCHED_SAMPLES:
        return None, None, len(matched)
    views = [float(max(0, s.views or 0)) for s in matched]
    return (
        float(median(views)),
        sum(1 for v in views if v >= VIEWS_CLEAR) / len(views),
        len(matched),
    )


async def _refresh_owner_profile(session: AsyncSession, ws: Workspace, provider: Any, now: datetime) -> None:
    my_handle = _norm(ws.my_x_handle)
    if not my_handle:
        return
    if ws.owner_profile_refreshed_at is not None and now - _utc(ws.owner_profile_refreshed_at) < OWNER_PROFILE_REFRESH:
        return
    try:
        profs = await provider.get_profiles_by_handles(session, ws, [my_handle])
    except (SpendCapExceededError, ProviderNotConfiguredError, httpx.HTTPError) as exc:
        logger.warning("Skipping owner profile refresh: %s", exc)
        return
    for prof in profs:
        if _norm(prof.handle) == my_handle:
            ws.owner_followers_count = int(prof.followers_count or 0) or ws.owner_followers_count
            ws.owner_is_premium = bool(prof.is_premium)
            ws.owner_profile_refreshed_at = now
            return


async def _collect_early_replies(
    session: AsyncSession,
    ws: Workspace,
    provider: Any,
    *,
    parent_x_post_id: str,
    parent_created_at: datetime,
    author_handle: str,
    max_pages: int,
    views_are_final: bool,
    known_reply_ids: set[str],
    now: datetime,
    expected_replies: int = 0,
) -> list[ReplyReachSample]:
    """Pages a post's replies and stores those posted within the early window. Stops once the
    first minutes are reached or the whole thread (`expected_replies`) has been read."""
    author = _norm(author_handle)
    owner = _norm(ws.my_x_handle)
    parent_utc = _utc(parent_created_at)
    added: list[ReplyReachSample] = []
    cursor: str | None = None
    seen = 0
    for _ in range(max(1, max_pages)):
        replies, cursor = await provider.get_tweet_replies(
            session, ws, parent_x_post_id, sort_by="Recency", cursor=cursor
        )
        if not replies:
            break
        seen += len(replies)
        reached_start = False
        for rep in replies:
            rep: ReplyRecord
            replier = _norm(rep.author_handle)
            if not replier or replier in (author, owner) or replier in PLATFORM_HANDLES:
                continue
            minutes_after = (_utc(rep.created_at_x) - parent_utc).total_seconds() / 60.0
            if minutes_after <= 2.0:
                reached_start = True
            if minutes_after < 0 or minutes_after > EARLY_WINDOW_MINUTES:
                continue
            if rep.x_post_id in known_reply_ids:
                continue
            known_reply_ids.add(rep.x_post_id)
            sample = ReplyReachSample(
                workspace_id=ws.id,
                author_handle=author,
                parent_x_post_id=parent_x_post_id,
                parent_created_at=parent_utc,
                reply_x_post_id=rep.x_post_id,
                replier_handle=replier,
                replier_x_user_id=str(rep.author_x_user_id or ""),
                replier_display_name=(rep.author_display_name or replier)[:160],
                replier_followers=int(rep.author_followers or 0),
                replier_premium=bool(rep.author_premium),
                reply_created_at=_utc(rep.created_at_x),
                minutes_after_parent=round(minutes_after, 2),
                views=int(rep.views or 0),
                matured=views_are_final,
                sampled_at=now,
                refreshed_at=now if views_are_final else None,
            )
            session.add(sample)
            added.append(sample)
        if not cursor or reached_start or (expected_replies > 0 and seen >= expected_replies):
            break
    return added


async def _known_reply_ids(session: AsyncSession, ws_id: int) -> set[str]:
    return set(
        (
            await session.execute(
                select(ReplyReachSample.reply_x_post_id).where(ReplyReachSample.workspace_id == ws_id)
            )
        )
        .scalars()
        .all()
    )


async def sample_tracked_accounts(
    session: AsyncSession, ws: Workspace, provider: Any, *, now: datetime, throttle_mode: str
) -> int:
    """Phase 1: for tracked accounts due a re-sample, read early replies on one young post each."""
    cap = TRACKED_POSTS_PER_RUN.get(throttle_mode, 0)
    if cap <= 0:
        return 0
    oldest = now - timedelta(minutes=TRACKED_POST_AGE_MIN[1])
    youngest = now - timedelta(minutes=TRACKED_POST_AGE_MIN[0])
    rows = (
        await session.execute(
            select(Post, TrackedAccount)
            .join(TrackedAccount, Post.author_account_id == TrackedAccount.id)
            .where(
                Post.workspace_id == ws.id,
                TrackedAccount.is_tracked.is_(True),
                TrackedAccount.is_muted.is_(False),
                Post.created_at_x >= oldest,
                Post.created_at_x <= youngest,
                Post.is_reply.is_(False),
                Post.is_retweet.is_(False),
                Post.latest_replies >= MIN_PARENT_REPLIES,
            )
            .order_by(Post.latest_views.desc())
        )
    ).all()
    known = await _known_reply_ids(session, ws.id)
    sampled_authors: set[int] = set()
    posts_sampled = 0
    for post, author in rows:
        if posts_sampled >= cap or author.id in sampled_authors:
            continue
        if author.reply_reach_sampled_at is not None and now - _utc(author.reply_reach_sampled_at) < TRACKED_RESAMPLE_AFTER:
            continue
        try:
            await _collect_early_replies(
                session, ws, provider,
                parent_x_post_id=post.x_post_id,
                parent_created_at=post.created_at_x,
                author_handle=author.handle,
                max_pages=PAGES_PER_TRACKED_POST,
                views_are_final=False,
                known_reply_ids=known,
                now=now,
                expected_replies=int(post.latest_replies or 0),
            )
        except SpendCapExceededError:
            break
        except (ProviderNotConfiguredError, httpx.HTTPError) as exc:
            logger.warning("Reply-reach sample failed for @%s: %s", author.handle, exc)
            continue
        author.reply_reach_sampled_at = now
        sampled_authors.add(author.id)
        posts_sampled += 1
    return posts_sampled


async def refresh_matured_samples(
    session: AsyncSession, ws: Workspace, provider: Any, *, now: datetime
) -> int:
    """Phase 2: read views on sampled replies once they are 24h+ old (200 per 1-unit batch)."""
    due = list(
        (
            await session.execute(
                select(ReplyReachSample)
                .where(
                    ReplyReachSample.workspace_id == ws.id,
                    ReplyReachSample.matured.is_(False),
                    ReplyReachSample.reply_created_at <= now - MATURE_AFTER,
                )
                .order_by(ReplyReachSample.reply_created_at.asc())
                .limit(REFRESH_BATCH * REFRESH_BATCHES_PER_RUN)
            )
        )
        .scalars()
        .all()
    )
    refreshed = 0
    for start in range(0, len(due), REFRESH_BATCH):
        batch = due[start : start + REFRESH_BATCH]
        try:
            recs = await provider.get_posts_by_ids(
                session, ws, [s.reply_x_post_id for s in batch], ledger_label=LEDGER_REFRESH
            )
        except SpendCapExceededError:
            break
        except (ProviderNotConfiguredError, httpx.HTTPError) as exc:
            logger.warning("Reply-reach maturing refresh failed: %s", exc)
            break
        views_by_id = {r.x_post_id: int(r.views or 0) for r in recs}
        for s in batch:
            # A reply missing from the response was deleted; keep its last known views
            s.views = max(int(s.views or 0), views_by_id.get(s.reply_x_post_id, 0))
            s.matured = True
            s.refreshed_at = now
            refreshed += 1
    return refreshed


def _pick_candidate_posts(
    posts: list[Any], now: datetime, *, author_id: str = "", author_handle: str = "", limit: int = 1
) -> list[Any]:
    lo, hi = CANDIDATE_POST_AGE
    handle = _norm(author_handle)

    def _by_author(p: Any) -> bool:
        # Timelines can carry other people's posts (pins, quotes); only the candidate's own count
        if author_id and str(p.author_x_user_id or "") == author_id:
            return True
        return bool(handle) and _norm(p.author_handle) == handle

    eligible = [
        p for p in posts
        if _by_author(p)
        and not p.is_reply and not p.is_retweet
        and int(p.replies or 0) >= MIN_PARENT_REPLIES
        and lo <= now - _utc(p.created_at_x) <= hi
    ]
    # Busiest threads that still fit in a page or two first; oversized threads last, smallest first
    def _order(p: Any) -> tuple[bool, int]:
        n = int(p.replies or 0)
        return (n > CANDIDATE_POST_MAX_REPLIES, -n if n <= CANDIDATE_POST_MAX_REPLIES else n)

    eligible.sort(key=_order)
    return eligible[: max(1, limit)]


async def test_candidates(
    session: AsyncSession,
    ws: Workspace,
    candidates: list[DiscoveredCandidate],
    *,
    now: datetime,
    throttle_mode: str,
) -> int:
    """Measures reply reach for the most relevant untested candidates before they're tracked."""
    cap = CANDIDATES_PER_SYNC.get(throttle_mode, 0)
    if cap <= 0 or ws.x_data_provider == "fixture" or not candidates:
        return 0
    try:
        provider = get_x_data_provider(ws.x_data_provider)
    except ProviderNotConfiguredError:
        return 0
    if not hasattr(provider, "get_tweet_replies"):
        return 0
    known = await _known_reply_ids(session, ws.id)
    tested = 0
    for cand in candidates:
        if tested >= cap:
            break
        if cand.reply_reach_sampled_at is not None and now - _utc(cand.reply_reach_sampled_at) < CANDIDATE_RETEST_AFTER:
            continue
        target = cand.x_user_id if (cand.x_user_id or "").isdigit() else cand.handle
        try:
            posts = await provider.get_user_tweets_and_replies(
                session, ws, target, count=40, ledger_label=LEDGER_CANDIDATE_TIMELINE
            )
            chosen = _pick_candidate_posts(
                posts,
                now,
                author_id=str(cand.x_user_id or "") if (cand.x_user_id or "").isdigit() else "",
                author_handle=cand.handle,
                limit=CANDIDATE_POSTS_PER_TEST,
            )
            for post in chosen:
                await _collect_early_replies(
                    session, ws, provider,
                    parent_x_post_id=post.x_post_id,
                    parent_created_at=post.created_at_x,
                    author_handle=cand.handle,
                    max_pages=PAGES_PER_CANDIDATE_POST,
                    views_are_final=True,
                    known_reply_ids=known,
                    now=now,
                    expected_replies=int(post.replies or 0),
                )
        except SpendCapExceededError:
            break
        except (ProviderNotConfiguredError, httpx.HTTPError) as exc:
            logger.warning("Candidate reply-reach test failed for @%s: %s", cand.handle, exc)
        cand.reply_reach_sampled_at = now
        tested += 1
    await session.flush()
    return tested


async def recompute_reach_estimates(session: AsyncSession, ws: Workspace, *, now: datetime) -> int:
    """Rewrites reply-reach fields on tracked accounts and candidates from matured samples."""
    since = now - timedelta(days=SAMPLE_WINDOW_DAYS)
    samples = list(
        (
            await session.execute(
                select(ReplyReachSample).where(
                    ReplyReachSample.workspace_id == ws.id,
                    ReplyReachSample.matured.is_(True),
                    ReplyReachSample.parent_created_at >= since,
                )
            )
        )
        .scalars()
        .all()
    )
    by_author: dict[str, list[ReplyReachSample]] = {}
    for s in samples:
        by_author.setdefault(_norm(s.author_handle), []).append(s)

    band = follower_band(ws)
    owner_premium = ws.owner_is_premium
    updated = 0
    for model in (TrackedAccount, DiscoveredCandidate):
        rows = list(
            (await session.execute(select(model).where(model.workspace_id == ws.id))).scalars().all()
        )
        for row in rows:
            med, share, n = estimate_from_samples(by_author.get(_norm(row.handle), []), band, owner_premium)
            row.reply_reach_median = med
            row.reply_reach_share_500 = share
            row.reply_reach_samples = n
            row.reply_reach_score = reply_reach_score(med, share)
            updated += 1 if med is not None else 0
    return updated


async def stage_thread_repliers(session: AsyncSession, ws: Workspace, *, now: datetime) -> int:
    """New candidates beyond the seeds' following lists: repliers whose early replies in tracked
    threads cleared 500 views, endorsed by the account whose thread it was."""
    since = now - timedelta(days=SAMPLE_WINDOW_DAYS)
    strong = list(
        (
            await session.execute(
                select(ReplyReachSample).where(
                    ReplyReachSample.workspace_id == ws.id,
                    ReplyReachSample.matured.is_(True),
                    ReplyReachSample.views >= THREAD_REPLIER_MIN_VIEWS,
                    ReplyReachSample.parent_created_at >= since,
                )
            )
        )
        .scalars()
        .all()
    )
    if not strong:
        return 0
    accts = list(
        (await session.execute(select(TrackedAccount).where(TrackedAccount.workspace_id == ws.id))).scalars().all()
    )
    acct_by_handle = {_norm(a.handle): a for a in accts}
    tracked_or_muted = {_norm(a.handle) for a in accts if a.is_tracked or a.is_muted}
    cands = list(
        (await session.execute(select(DiscoveredCandidate).where(DiscoveredCandidate.workspace_id == ws.id))).scalars().all()
    )
    cand_by_handle = {_norm(c.handle): c for c in cands}
    existing_ends = {
        (e.candidate_id, e.endorser_account_id, e.source_x_post_id)
        for e in (
            await session.execute(select(PeerEndorsement).where(PeerEndorsement.workspace_id == ws.id))
        ).scalars().all()
    }
    owner = _norm(ws.my_x_handle)
    staged = 0
    for s in sorted(strong, key=lambda x: -int(x.views or 0)):
        if staged >= THREAD_REPLIERS_PER_RUN:
            break
        replier = _norm(s.replier_handle)
        endorser = acct_by_handle.get(_norm(s.author_handle))
        if (
            not replier or replier == owner or replier in tracked_or_muted
            or replier in PLATFORM_HANDLES or endorser is None or not endorser.is_tracked
        ):
            continue
        cand = cand_by_handle.get(replier)
        if cand is None:
            foll = int(s.replier_followers or 0)
            cand = DiscoveredCandidate(
                workspace_id=ws.id,
                x_user_id=s.replier_x_user_id or replier,
                handle=replier,
                display_name=s.replier_display_name or replier,
                followers_count=foll,
                status="active" if foll < COLD_START_FOLLOWER_CAP else "over_cap",
            )
            session.add(cand)
            await session.flush()
            cand_by_handle[replier] = cand
            staged += 1
        elif cand.status == "dismissed":
            continue
        key = (cand.id, endorser.id, s.reply_x_post_id)
        if key in existing_ends:
            continue
        session.add(
            PeerEndorsement(
                workspace_id=ws.id,
                candidate_id=cand.id,
                endorser_account_id=endorser.id,
                endorsement_type="reply",
                source_x_post_id=s.reply_x_post_id,
                weight_contribution=1.0,
                occurred_at=_utc(s.reply_created_at),
            )
        )
        existing_ends.add(key)
    await session.flush()
    return staged


async def run_reply_reach_cycle(
    session: AsyncSession, ws: Workspace, *, now: datetime, throttle_mode: str
) -> dict[str, int]:
    """Hourly: owner profile (daily), sample young tracked posts, read matured views, recompute
    estimates, and stage strong thread repliers as candidates."""
    summary = {"posts_sampled": 0, "samples_matured": 0, "accounts_estimated": 0, "repliers_staged": 0}
    if ws.x_data_provider == "fixture" or throttle_mode == "paused":
        return summary
    try:
        provider = get_x_data_provider(ws.x_data_provider)
    except ProviderNotConfiguredError:
        return summary
    if not hasattr(provider, "get_tweet_replies"):
        return summary
    await _refresh_owner_profile(session, ws, provider, now)
    summary["posts_sampled"] = await sample_tracked_accounts(
        session, ws, provider, now=now, throttle_mode=throttle_mode
    )
    summary["samples_matured"] = await refresh_matured_samples(session, ws, provider, now=now)
    await session.flush()
    summary["accounts_estimated"] = await recompute_reach_estimates(session, ws, now=now)
    summary["repliers_staged"] = await stage_thread_repliers(session, ws, now=now)
    await session.flush()
    return summary
