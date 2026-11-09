"""Empirical Per-Account Reply/Quote Yield Learning & Follower-Growth Attribution Engine.

Implements Issue #1 (v2) direction:
- Seeds are a 28-day bootstrap only (`SEED_BOOTSTRAP_GRACE_DAYS = 28`).
- Measures per-account reply/quote yield from `/tw-v2/user/tweets-and-replies` + `OwnFollowerSnapshot`
  (~1-2 API units per sync) and reconciles actions clicked in Hot Posts (`acted`).
- Blends PR #2 SimClusters affinity + reach prior (`PriorScore`) with observed average reply views
  and attributed follower gains (`EmpiricalYield`) via Empirical Bayes shrinkage.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import math
import re
from statistics import median
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tweettracker.models import (
    DiscoveredCandidate,
    OwnFollowerSnapshot,
    OwnReplyOutcome,
    Post,
    TrackedAccount,
    Workspace,
)
from tweettracker.providers import (
    ProviderNotConfiguredError,
    SpendCapExceededError,
    get_x_data_provider,
)

logger = logging.getLogger(__name__)

SEED_BOOTSTRAP_GRACE_DAYS: int = 28
SEED_BOOTSTRAP_GRACE_HOURS: float = float(SEED_BOOTSTRAP_GRACE_DAYS * 24)
MIN_ACTIVE_CORE_FLOOR: int = 10

YIELD_PRIOR_PSEUDO_COUNT: float = 2.0
YIELD_RECENCY_HALF_LIFE_DAYS: float = 30.0
SEED_IDLE_DECAY_PER_WEEK: float = 1.75

FOLLOWER_ATTRIBUTION_WINDOW_HOURS: float = 24.0
FOLLOWER_ATTRIBUTION_DECAY_HOURS: float = 6.0
MAX_SNAPSHOT_GAP_HOURS: float = 72.0
MIN_REF_REPLY_VIEWS: float = 400.0

_LEADING_MENTION_RE = re.compile(r"(?:^|\s)@([A-Za-z0-9_]{1,15})\b")


def _ensure_utc(dt: datetime | None, fallback: datetime | None = None) -> datetime:
    if dt is None:
        return fallback if fallback is not None else datetime.now(timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _norm_handle(raw: str | None) -> str:
    return (raw or "").strip().lstrip("@").strip().lower()


@dataclass(slots=True)
class ReplyOutcomeSample:
    x_reply_post_id: str
    target_handle: str
    created_at_x: datetime
    views: int
    attributed_follows_gained: float = 0.0
    action_kind: str = "reply"
    was_mutual_at_reply: bool = False


@dataclass(slots=True)
class AccountYieldMetrics:
    prior_score: float
    empirical_yield: float
    yield_score: float
    reply_sample_count: int
    eff_sample_weight: float
    avg_reply_views: float
    attributed_follows_30d: float


def compute_reach_prior_score(
    *,
    core_affinity_score: float,
    followers_count: int = 0,
    has_recent_climbing_post: bool = False,
    is_mutual: bool = False,
    follows_me: bool = False,
) -> float:
    """Cold-start expected reach prior in [0, 1] before sufficient reply outcome data exists.

    Starts from PR #2's SimClusters + UserCredV2 `core_affinity_score` and modulates it by
    audience reach, active climbing Hot Posts, and the 20x Heavy Ranker mutual reply boost.
    Scaling multiplicatively by `core_affinity_score` guarantees unconnected mega-accounts stay
    below the 0.55 add threshold and decayed co-follow accounts settle below 0.32.
    """
    aff = min(1.0, max(0.0, float(core_affinity_score)))
    if aff <= 0.0:
        return 0.0

    aud_norm = min(
        1.0,
        max(0.0, math.log10(max(int(followers_count), 100) / 500.0) / 3.5),
    )
    hot_bonus = 0.08 if has_recent_climbing_post else 0.0
    mut_bonus = 0.10 if is_mutual else (0.04 if follows_me else 0.0)

    raw = aff * (0.98 + 0.12 * aud_norm + hot_bonus + mut_bonus)
    return round(min(1.0, max(0.0, raw)), 4)


def attribute_follower_gains_in_memory(
    snapshots: Sequence[tuple[datetime, int]],
    outcomes: Sequence[ReplyOutcomeSample],
) -> dict[str, float]:
    """Splits net follower gains across active own replies/quotes within [0h, 24h] windows.

    Overlapping replies share each interval's positive follower delta proportionally to
    `max(10, views) * exp(-lag_hours / 6h)`, conserving total gained followers without double-counting.
    """
    attributed: dict[str, float] = {o.x_reply_post_id: 0.0 for o in outcomes}
    if len(snapshots) < 2 or not outcomes:
        return attributed

    ordered_snaps = sorted(
        ((_ensure_utc(ts), int(cnt)) for ts, cnt in snapshots),
        key=lambda item: item[0],
    )
    ordered_outcomes = [
        (o, _ensure_utc(o.created_at_x)) for o in outcomes if o.x_reply_post_id
    ]
    window_td = timedelta(hours=FOLLOWER_ATTRIBUTION_WINDOW_HOURS)

    for idx in range(1, len(ordered_snaps)):
        t_prev, f_prev = ordered_snaps[idx - 1]
        t_curr, f_curr = ordered_snaps[idx]
        delta_f = f_curr - f_prev
        if delta_f <= 0 or t_curr <= t_prev:
            continue
        gap_hours = (t_curr - t_prev).total_seconds() / 3600.0
        if gap_hours > MAX_SNAPSHOT_GAP_HOURS:
            continue

        active_weights: list[tuple[str, float]] = []
        for out_sample, t_reply in ordered_outcomes:
            if t_reply > t_curr or (t_reply + window_td) < t_prev:
                continue
            eff_start = max(t_prev, t_reply)
            eff_mid_ts = 0.5 * (eff_start.timestamp() + t_curr.timestamp())
            lag_hours = max(0.0, (eff_mid_ts - t_reply.timestamp()) / 3600.0)
            time_w = math.exp(-lag_hours / FOLLOWER_ATTRIBUTION_DECAY_HOURS)
            imp_w = max(10.0, float(max(0, out_sample.views)))
            w = imp_w * time_w
            if w > 0.0:
                active_weights.append((out_sample.x_reply_post_id, w))

        total_w = sum(w for _, w in active_weights)
        if total_w > 1e-9:
            for reply_id, w in active_weights:
                attributed[reply_id] = attributed.get(reply_id, 0.0) + float(delta_f) * (
                    w / total_w
                )

    return {k: round(v, 4) for k, v in attributed.items()}


def compute_workspace_reference_reply_views(
    outcomes: Sequence[ReplyOutcomeSample],
) -> float:
    """Computes workspace baseline reply impressions (floored at MIN_REF_REPLY_VIEWS = 400)."""
    positive_views = [float(o.views) for o in outcomes if o.views > 0]
    if not positive_views:
        return MIN_REF_REPLY_VIEWS
    return max(MIN_REF_REPLY_VIEWS, float(median(positive_views)))


def compute_account_yield_metrics(
    *,
    prior_score: float,
    outcomes: Sequence[ReplyOutcomeSample],
    workspace_ref_views: float = MIN_REF_REPLY_VIEWS,
    is_mutual: bool = False,
    is_auto_tracked: bool = False,
    created_or_added_at: datetime | None = None,
    has_recent_climbing_post: bool = False,
    reference_time: datetime | None = None,
) -> AccountYieldMetrics:
    """Computes Empirical Bayes `yield_score` in [0, 1] from reply views, follows gained, and 28d seed decay."""
    now_utc = _ensure_utc(reference_time)
    cutoff_30d = now_utc - timedelta(days=30)
    ref_views = max(MIN_REF_REPLY_VIEWS, float(workspace_ref_views))

    eff_weight_sum = 0.0
    weighted_views_sum = 0.0
    weighted_follows_sum = 0.0
    follows_30d = 0.0
    sample_cnt = len(outcomes)

    for sample in outcomes:
        created_utc = _ensure_utc(sample.created_at_x, now_utc)
        age_days = max(0.0, (now_utc - created_utc).total_seconds() / 86_400.0)
        recency_w = math.pow(2.0, -age_days / YIELD_RECENCY_HALF_LIFE_DAYS)
        eff_weight_sum += recency_w
        weighted_views_sum += recency_w * float(max(0, sample.views))
        weighted_follows_sum += recency_w * max(0.0, float(sample.attributed_follows_gained))
        if created_utc >= cutoff_30d:
            follows_30d += max(0.0, float(sample.attributed_follows_gained))

    if eff_weight_sum > 1e-9:
        avg_views = weighted_views_sum / eff_weight_sum
        avg_follows = weighted_follows_sum / eff_weight_sum
        log_ratio = math.log((avg_views + 25.0) / (ref_views + 25.0))
        y_views = 1.0 / (1.0 + math.exp(-1.35 * log_ratio))
        y_follows = 0.35 * (1.0 - math.exp(-1.5 * avg_follows))
        recip_bonus = 0.06 if is_mutual else 0.0
        emp_yield = min(1.0, max(0.0, y_views + y_follows + recip_bonus))
    else:
        avg_views = 0.0
        emp_yield = 0.0

    # Passive dormancy decay after 28-day bootstrap grace period for seeds that never produce
    # climbing Hot Posts or reply reach (auto-adds already decay after 48h in nmf_simclusters).
    n_idle = 0.0
    if not is_auto_tracked and created_or_added_at is not None:
        anchor_ts = _ensure_utc(created_or_added_at, now_utc)
        age_days_tracked = max(0.0, (now_utc - anchor_ts).total_seconds() / 86_400.0)
        if (
            age_days_tracked > float(SEED_BOOTSTRAP_GRACE_DAYS)
            and not has_recent_climbing_post
            and (eff_weight_sum < 0.25 or emp_yield < 0.32)
        ):
            weeks_past_grace = (age_days_tracked - float(SEED_BOOTSTRAP_GRACE_DAYS)) / 7.0
            n_idle = weeks_past_grace * SEED_IDLE_DECAY_PER_WEEK

    denom = max(YIELD_PRIOR_PSEUDO_COUNT + eff_weight_sum + n_idle, 1e-9)
    posterior = (YIELD_PRIOR_PSEUDO_COUNT * float(prior_score) + eff_weight_sum * emp_yield) / denom
    final_yield = round(min(1.0, max(0.0, posterior)), 4)

    return AccountYieldMetrics(
        prior_score=round(float(prior_score), 4),
        empirical_yield=round(emp_yield, 4),
        yield_score=final_yield,
        reply_sample_count=sample_cnt,
        eff_sample_weight=round(eff_weight_sum, 4),
        avg_reply_views=round(avg_views, 1),
        attributed_follows_30d=round(follows_30d, 2),
    )


async def attribute_workspace_follower_gains(
    session: AsyncSession,
    workspace_id: int,
    *,
    now: datetime | None = None,
) -> int:
    """Recomputes `attributed_follows_gained` across recent `OwnReplyOutcome` rows."""
    now_utc = _ensure_utc(now)
    cutoff = now_utc - timedelta(days=40)

    snaps = list(
        (
            await session.execute(
                select(OwnFollowerSnapshot)
                .where(
                    OwnFollowerSnapshot.workspace_id == workspace_id,
                    OwnFollowerSnapshot.recorded_at >= cutoff,
                )
                .order_by(OwnFollowerSnapshot.recorded_at.asc())
            )
        )
        .scalars()
        .all()
    )
    outcomes = list(
        (
            await session.execute(
                select(OwnReplyOutcome)
                .where(
                    OwnReplyOutcome.workspace_id == workspace_id,
                    OwnReplyOutcome.created_at_x >= cutoff,
                )
                .order_by(OwnReplyOutcome.created_at_x.asc())
            )
        )
        .scalars()
        .all()
    )
    if len(snaps) < 2 or not outcomes:
        return 0

    snap_tuples = [(_ensure_utc(s.recorded_at, now_utc), int(s.followers_count)) for s in snaps]
    samples = [
        ReplyOutcomeSample(
            x_reply_post_id=o.x_reply_post_id,
            target_handle=o.target_handle,
            created_at_x=_ensure_utc(o.created_at_x, now_utc),
            views=int(o.latest_views or 0),
            action_kind=o.action_kind,
            was_mutual_at_reply=bool(o.was_mutual_at_reply),
        )
        for o in outcomes
    ]
    credit_map = attribute_follower_gains_in_memory(snap_tuples, samples)
    updated = 0
    for row in outcomes:
        new_val = float(credit_map.get(row.x_reply_post_id, 0.0))
        if abs(float(row.attributed_follows_gained or 0.0) - new_val) > 1e-6:
            row.attributed_follows_gained = new_val
            updated += 1
    await session.flush()
    return updated


async def sync_own_reply_yield(
    session: AsyncSession,
    workspace: Workspace,
    *,
    throttle_mode: str = "normal",
    now: datetime | None = None,
) -> dict[str, int]:
    """Ingests own replies/quotes + follower snapshot via TweetAPI (~1-2 units/sync)."""
    now_utc = _ensure_utc(now)
    my_handle = _norm_handle(workspace.my_x_handle)
    if not my_handle or workspace.x_data_provider == "fixture" or throttle_mode == "paused":
        return {"replies_upserted": 0, "replies_rehydrated": 0, "follower_snapshot_recorded": 0}

    try:
        provider = get_x_data_provider(workspace.x_data_provider)
    except ProviderNotConfiguredError:
        return {"replies_upserted": 0, "replies_rehydrated": 0, "follower_snapshot_recorded": 0}

    # 1. Fetch latest timeline + replies for `@my_x_handle` (1 unit)
    try:
        own_posts = await provider.get_user_tweets_and_replies(
            session, workspace, my_handle, count=40
        )
    except (SpendCapExceededError, ProviderNotConfiguredError, httpx.HTTPError) as exc:
        logger.warning("Skipping own reply yield sync for @%s: %s", my_handle, exc)
        return {"replies_upserted": 0, "replies_rehydrated": 0, "follower_snapshot_recorded": 0}

    # Snapshot own followers_count directly from returned author metadata (0 extra units!)
    my_followers = 0
    for rec in own_posts:
        if _norm_handle(rec.author_handle) == my_handle and int(rec.author_followers or 0) > my_followers:
            my_followers = int(rec.author_followers)

    if my_followers <= 0 and not own_posts and throttle_mode == "normal":
        try:
            profs = await provider.get_profiles_by_handles(session, workspace, [my_handle])
            for p in profs:
                if _norm_handle(p.handle) == my_handle and p.followers_count > 0:
                    my_followers = int(p.followers_count)
                    break
        except (SpendCapExceededError, ProviderNotConfiguredError, httpx.HTTPError):
            pass

    snap_recorded = 0
    if my_followers > 0:
        latest_snap = (
            await session.execute(
                select(OwnFollowerSnapshot)
                .where(OwnFollowerSnapshot.workspace_id == workspace.id)
                .order_by(OwnFollowerSnapshot.recorded_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if (
            latest_snap is not None
            and (now_utc - _ensure_utc(latest_snap.recorded_at, now_utc)) < timedelta(minutes=5)
        ):
            latest_snap.followers_count = my_followers
        else:
            session.add(
                OwnFollowerSnapshot(
                    workspace_id=workspace.id,
                    recorded_at=now_utc,
                    followers_count=my_followers,
                )
            )
            snap_recorded = 1

    # Load workspace accounts and posts to link reply/quote targets & Hot Post actions
    all_accts = list(
        (
            await session.execute(
                select(TrackedAccount).where(TrackedAccount.workspace_id == workspace.id)
            )
        )
        .scalars()
        .all()
    )
    muted_handles = {_norm_handle(a.handle) for a in all_accts if bool(a.is_muted)}
    acct_by_handle = {
        _norm_handle(a.handle): a
        for a in all_accts
        if bool(a.is_tracked) and not bool(a.is_muted)
    }
    acct_by_id = {a.id: a for a in all_accts}

    ws_posts = list(
        (
            await session.execute(
                select(Post).where(Post.workspace_id == workspace.id)
            )
        )
        .scalars()
        .all()
    )
    post_by_x_id = {p.x_post_id.strip(): p for p in ws_posts if p.x_post_id}
    acted_posts_by_author: dict[int, list[Post]] = defaultdict(list)
    for p in ws_posts:
        if p.queue_status == "acted":
            acted_posts_by_author[p.author_account_id].append(p)

    existing_outcomes = list(
        (
            await session.execute(
                select(OwnReplyOutcome).where(OwnReplyOutcome.workspace_id == workspace.id)
            )
        )
        .scalars()
        .all()
    )
    outcome_by_x_id = {o.x_reply_post_id: o for o in existing_outcomes}

    seen_reply_ids: set[str] = set()
    upserted = 0

    for rec in own_posts:
        if rec.is_retweet or not (rec.is_reply or rec.is_quote):
            continue
        # The tweets-and-replies timeline also carries other people's posts (e.g. ones you
        # retweeted, surfaced as their quote/reply); only count posts you actually wrote.
        if _norm_handle(rec.author_handle) != my_handle:
            continue
        reply_x_id = (rec.x_post_id or "").strip()
        if not reply_x_id:
            continue

        rec_created = _ensure_utc(rec.created_at_x, now_utc)
        target_x_post_id = (
            (rec.quoted_post_id if rec.is_quote else rec.in_reply_to_post_id)
            or rec.in_reply_to_post_id
            or rec.quoted_post_id
        )
        source_post = post_by_x_id.get(target_x_post_id.strip()) if target_x_post_id else None

        target_h = _norm_handle(rec.target_author_handle)
        if not target_h and source_post is not None:
            src_author = acct_by_id.get(source_post.author_account_id)
            if src_author is not None:
                target_h = _norm_handle(src_author.handle)
        if not target_h and rec.is_reply and rec.text:
            m = _LEADING_MENTION_RE.search(rec.text)
            if m:
                target_h = _norm_handle(m.group(1))

        if not target_h or target_h == my_handle:
            continue

        target_acct = acct_by_handle.get(target_h)
        if source_post is None and target_acct is not None:
            for cand_post in acted_posts_by_author.get(target_acct.id, ()):
                p_created = _ensure_utc(cand_post.created_at_x, now_utc)
                if p_created <= rec_created <= p_created + timedelta(hours=12):
                    source_post = cand_post
                    break

        action_kind = "quote" if rec.is_quote else "reply"
        is_matured = (now_utc - rec_created) >= timedelta(hours=24)

        row = outcome_by_x_id.get(reply_x_id)
        if row is None:
            age_at_reply: int | None = None
            breakout_at_reply: float | None = None
            from_hot = False
            if source_post is not None:
                p_created = _ensure_utc(source_post.created_at_x, rec_created)
                age_at_reply = max(0, int((rec_created - p_created).total_seconds() / 60.0))
                breakout_at_reply = float(source_post.breakout_score or 0.0)
                from_hot = source_post.queue_status == "acted"

            was_mutual = bool(
                target_acct is not None and target_acct.follows_me and target_acct.followed_by_me
            )
            row = OwnReplyOutcome(
                workspace_id=workspace.id,
                x_reply_post_id=reply_x_id,
                target_account_id=(target_acct.id if target_acct is not None else None),
                target_handle=target_h,
                source_post_id=(source_post.id if source_post is not None else None),
                target_x_post_id=target_x_post_id,
                action_kind=action_kind,
                from_hot_posts=from_hot,
                created_at_x=rec_created,
                post_age_at_reply_min=age_at_reply,
                post_breakout_at_reply=breakout_at_reply,
                was_mutual_at_reply=was_mutual,
                latest_views=max(0, int(rec.views or 0)),
                latest_likes=max(0, int(rec.likes or 0)),
                latest_replies=max(0, int(rec.replies or 0)),
                latest_quotes=max(0, int(rec.quotes or 0)),
                latest_bookmarks=max(0, int(rec.bookmarks or 0)),
                matured_24h=is_matured,
                last_refreshed_at=now_utc,
            )
            session.add(row)
            outcome_by_x_id[reply_x_id] = row
        else:
            if row.target_account_id is None and target_acct is not None:
                row.target_account_id = target_acct.id
            row.latest_views = max(int(row.latest_views or 0), int(rec.views or 0))
            row.latest_likes = max(int(row.latest_likes or 0), int(rec.likes or 0))
            row.latest_replies = max(int(row.latest_replies or 0), int(rec.replies or 0))
            row.latest_quotes = max(int(row.latest_quotes or 0), int(rec.quotes or 0))
            row.latest_bookmarks = max(int(row.latest_bookmarks or 0), int(rec.bookmarks or 0))
            if is_matured:
                row.matured_24h = True
            row.last_refreshed_at = now_utc

        seen_reply_ids.add(reply_x_id)
        upserted += 1

    # Stage any brand-new untracked reply targets into DiscoveredCandidate so high-yield
    # direct replies on X can promote creators even before a 2-hop crawl reaches them.
    untracked_targets = {
        _norm_handle(o.target_handle)
        for o in outcome_by_x_id.values()
        if o.x_reply_post_id in seen_reply_ids
        and _norm_handle(o.target_handle) not in acct_by_handle
        and _norm_handle(o.target_handle) not in muted_handles
        and _norm_handle(o.target_handle) != my_handle
    }
    if untracked_targets and throttle_mode != "paused":
        existing_cand_handles = {
            _norm_handle(h)
            for h in (
                await session.execute(
                    select(DiscoveredCandidate.handle).where(
                        DiscoveredCandidate.workspace_id == workspace.id
                    )
                )
            )
            .scalars()
            .all()
        }
        missing_cand_handles = sorted(untracked_targets - existing_cand_handles)
        if missing_cand_handles:
            try:
                target_profs = await provider.get_profiles_by_handles(
                    session, workspace, missing_cand_handles
                )
                for prof in target_profs:
                    ph = _norm_handle(prof.handle)
                    if not ph or ph in existing_cand_handles:
                        continue
                    foll = max(0, int(prof.followers_count or 0))
                    session.add(
                        DiscoveredCandidate(
                            workspace_id=workspace.id,
                            x_user_id=str(prof.x_user_id or ph),
                            handle=ph,
                            display_name=prof.display_name or ph,
                            bio=prof.bio or "",
                            avatar_url=prof.avatar_url,
                            followers_count=foll,
                            following_count=max(0, int(prof.following_count or 0)),
                            status=("active" if foll < 50_000 else "over_cap"),
                        )
                    )
                    existing_cand_handles.add(ph)
            except (SpendCapExceededError, ProviderNotConfiguredError, httpx.HTTPError):
                pass

    # 2. Optional 1-unit batch refresh for maturing (2h-48h) own replies that scrolled off page 1
    stale_ids = [
        o.x_reply_post_id
        for o in outcome_by_x_id.values()
        if o.x_reply_post_id not in seen_reply_ids
        and not bool(o.matured_24h)
        and timedelta(hours=2)
        <= (now_utc - _ensure_utc(o.created_at_x, now_utc))
        <= timedelta(hours=48)
    ]
    rehydrated = 0
    if stale_ids and throttle_mode == "normal":
        try:
            refreshed_recs = await provider.get_posts_by_ids(
                session, workspace, stale_ids[:200]
            )
            for rec in refreshed_recs:
                row = outcome_by_x_id.get(rec.x_post_id)
                if row is None:
                    continue
                if _norm_handle(rec.author_handle) != my_handle:
                    # Recorded before the author check existed; not a post you wrote
                    await session.delete(row)
                    outcome_by_x_id.pop(rec.x_post_id, None)
                    continue
                row.latest_views = max(int(row.latest_views or 0), int(rec.views or 0))
                row.latest_likes = max(int(row.latest_likes or 0), int(rec.likes or 0))
                row.latest_replies = max(int(row.latest_replies or 0), int(rec.replies or 0))
                row.latest_quotes = max(int(row.latest_quotes or 0), int(rec.quotes or 0))
                row.latest_bookmarks = max(int(row.latest_bookmarks or 0), int(rec.bookmarks or 0))
                if (now_utc - _ensure_utc(row.created_at_x, now_utc)) >= timedelta(hours=24):
                    row.matured_24h = True
                row.last_refreshed_at = now_utc
                rehydrated += 1
        except (SpendCapExceededError, ProviderNotConfiguredError, httpx.HTTPError) as exc:
            logger.warning("Skipping maturing own-reply batch refresh: %s", exc)

    await session.flush()
    await attribute_workspace_follower_gains(session, workspace.id, now=now_utc)
    return {
        "replies_upserted": upserted,
        "replies_rehydrated": rehydrated,
        "follower_snapshot_recorded": snap_recorded,
    }
