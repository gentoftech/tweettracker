"""Epic 4 (<50k) Rising Voice Discovery, Punch-Above-Weight, & SALSA Peer Co-Sign Scorer."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import math
from typing import Any

import numpy as np
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from tweettracker.algo import (
    FilterCandidate,
    PipelineParams,
    UserCredV2,
    sample_cold_start_reward,
    shrink_propensity,
)
from tweettracker.graph.builder import (
    FOLLOW_WEIGHT,
    MUTUAL_REPLY_WEIGHT,
    QUOTE_WEIGHT,
    REPLY_WEIGHT,
    REPOST_WEIGHT,
    decay_multiplier,
)
from tweettracker.graph.nmf_simclusters import (
    BOOTSTRAP_GRACE_HOURS,
    build_core_graph_context,
    score_behavioral_core_affinity,
)
from tweettracker.graph.reply_yield import (
    MIN_ACTIVE_CORE_FLOOR,
    ReplyOutcomeSample,
    compute_account_yield_metrics,
    compute_reach_prior_score,
    compute_workspace_reference_reply_views,
    sync_own_reply_yield,
)
from tweettracker.models import (
    AccountCommunityMembership,
    Community,
    DiscoveredCandidate,
    OwnReplyOutcome,
    PeerEndorsement,
    Post,
    PostInteraction,
    RelationshipEdge,
    TrackedAccount,
    Workspace,
)
from tweettracker.providers import (
    ProviderNotConfiguredError,
    SpendCapExceededError,
    estimate_workspace_monthly_units,
    get_x_data_provider,
)

COLD_START_FOLLOWER_CAP: int = PipelineParams().cold_start_follower_cap  # 50_000
MIN_SAMPLED_ORIGINAL_POSTS: int = 5
MIN_POSTS_ABOVE_3X: int = 3
BREAKOUT_REACH_RATIO_THRESHOLD: float = 3.0

ENDORSEMENT_SIGNAL_WEIGHTS: dict[str, float] = {
    "follow": 3.0,
    "quote": 2.0,
    "reply": 1.0,
}

FOLLOWER_BRACKET_RANGES: dict[str, tuple[int, int]] = {
    "all": (0, COLD_START_FOLLOWER_CAP),
    "under_2500": (0, 2_500),
    "2500_10k": (2_500, 10_000),
    "10k_25k": (10_000, 25_000),
    "25k_50k": (25_000, COLD_START_FOLLOWER_CAP),
}

SNAPSHOT_AGE_FRACTIONS: dict[str, float] = {
    "15": 0.09,
    "30": 0.17,
    "45": 0.25,
    "60": 0.33,
    "120": 0.50,
    "360": 0.72,
    "1440": 0.91,
    "2880": 1.00,
}


@dataclass(slots=True)
class CandidateSamplePost:
    """Normalized post sample for evaluating <50k creator consistency & outlier reach."""

    x_post_id: str
    text: str
    views: int
    likes: int = 0
    replies: int = 0
    quotes: int = 0
    reposts: int = 0
    bookmarks: int = 0
    is_reply: bool = False
    is_retweet: bool = False
    is_quote: bool = False


@dataclass(slots=True)
class SampledPostsSummary:
    sampled_posts_count: int
    posts_above_3x_count: int
    median_reach_multiple: float
    cold_start_propensity_lift: float
    top_posts_json: list[dict[str, Any]]


@dataclass(slots=True)
class EndorsementScoringItem:
    endorsement_id: int | None
    endorser_account_id: int
    endorsement_type: str
    heavy_hitter_score: float
    salsa_scout: float


def _is_active_membership(m: AccountCommunityMembership) -> bool:
    return bool(m.is_manual_tag) or (
        max(float(m.nmf_interested_in or 0.0), float(m.nmf_known_for or 0.0)) >= 0.25
    )


def classify_follower_bracket(followers_count: int) -> str:
    """Maps follower count into Epic 4 bracket key."""
    if followers_count < 2_500:
        return "under_2500"
    if followers_count < 10_000:
        return "2500_10k"
    if followers_count < 25_000:
        return "10k_25k"
    if followers_count < COLD_START_FOLLOWER_CAP:
        return "25k_50k"
    return "over_50k"


def matches_follower_bracket(
    followers_count: int,
    bracket: str | None = None,
    *,
    follower_cap: int = COLD_START_FOLLOWER_CAP,
) -> bool:
    """Checks strict <50k cap plus optional follower bracket slice."""
    if followers_count < 0 or followers_count >= follower_cap:
        return False
    norm = (bracket or "all").strip().lower()
    if norm in ("", "all"):
        return True
    bounds = FOLLOWER_BRACKET_RANGES.get(norm)
    if bounds is None:
        return False
    lo, hi = bounds
    return lo <= followers_count < min(hi, follower_cap)


def _deterministic_rng(seed_key: str) -> np.random.Generator:
    digest = hashlib.blake2b(seed_key.encode("utf-8"), digest_size=8).digest()
    return np.random.default_rng(int.from_bytes(digest, "little"))


def compute_cold_start_propensity_lift(
    *,
    followers_count: int,
    views: int,
    likes: int | None = None,
    seed_key: str = "cold_start",
) -> float:
    """Evaluates AuthorColdStart Thompson-Sampling reward + Empirical-Bayes engagement lift."""
    eff_views = max(int(views), 1)
    est_likes = int(likes) if likes is not None else max(1, int(eff_views * 0.022))

    # Scale onto ColdStart window (<200 impressions) while preserving observed rate
    scaled_home_views = min(195, max(20, eff_views // 25))
    eb_fav_rate = shrink_propensity(est_likes, eff_views, prior_rate=0.015, alpha_0=150.0)
    scaled_favs = max(0, int(round(eb_fav_rate * scaled_home_views)))

    fc = FilterCandidate(
        tweet_id=1,
        author_id=1,
        in_reply_to_tweet_id=None,
        retweeted_tweet_id=None,
        age_secs=1_800.0,
        author_followers_count=min(followers_count, COLD_START_FOLLOWER_CAP),
        view_count_on_home=scaled_home_views,
        fav_count=scaled_favs,
    )
    rng = _deterministic_rng(f"{seed_key}:{followers_count}:{eff_views}:{est_likes}")
    draws = [
        sample_cold_start_reward(fc, alpha0=0.75, beta_0=49.25, scale=1.0, rng=rng)
        for _ in range(16)
    ]
    ts_mean = float(np.mean(draws))
    return round(0.65 * ts_mean + 0.35 * eb_fav_rate, 6)


def evaluate_sampled_posts(
    posts: Sequence[CandidateSamplePost],
    followers_count: int,
    *,
    seed_key: str = "candidate",
) -> SampledPostsSummary:
    """Evaluates original (!is_reply && !is_retweet) post samples against >=3.0x reach floor."""
    originals = [p for p in posts if (not p.is_reply) and (not p.is_retweet)]
    denom = float(max(followers_count, 100))
    if not originals:
        return SampledPostsSummary(
            sampled_posts_count=0,
            posts_above_3x_count=0,
            median_reach_multiple=0.0,
            cold_start_propensity_lift=0.015,
            top_posts_json=[],
        )

    multiples: list[float] = []
    lifts: list[float] = []
    scored_rows: list[tuple[float, CandidateSamplePost]] = []

    for idx, p in enumerate(originals):
        mult = max(0.0, float(p.views) / denom)
        multiples.append(mult)
        lifts.append(
            compute_cold_start_propensity_lift(
                followers_count=followers_count,
                views=p.views,
                likes=p.likes,
                seed_key=f"{seed_key}:{p.x_post_id}:{idx}",
            )
        )
        scored_rows.append((mult, p))

    sampled_cnt = len(originals)
    above_3x_cnt = sum(1 for m in multiples if m >= BREAKOUT_REACH_RATIO_THRESHOLD)
    med_mult = round(float(np.median(multiples)), 2)
    avg_lift = float(np.mean(lifts)) if lifts else 0.015

    scored_rows.sort(key=lambda pair: (-pair[0], -pair[1].views))
    top_posts = [
        {
            "x_post_id": post.x_post_id,
            "text": post.text,
            "views": int(post.views),
            "reach_multiple": round(mult, 2),
        }
        for mult, post in scored_rows[:3]
    ]

    return SampledPostsSummary(
        sampled_posts_count=sampled_cnt,
        posts_above_3x_count=above_3x_cnt,
        median_reach_multiple=med_mult,
        cold_start_propensity_lift=avg_lift,
        top_posts_json=top_posts,
    )


def compute_punch_above_weight_score(
    *,
    followers_count: int,
    sampled_posts_count: int,
    posts_above_3x_count: int,
    median_reach_multiple: float,
    followers_growth_rate: float,
    cold_start_propensity_lift: float | None = None,
    follower_cap: int = COLD_START_FOLLOWER_CAP,
    seed_key: str = "paw",
) -> float:
    """Computes Story 4.1 Punch-Above-Weight Score (0..100) with one-hit-wonder penalty."""
    if followers_count < 0 or followers_count >= follower_cap or sampled_posts_count <= 0:
        return 0.0

    eff_above_3x = max(0, min(posts_above_3x_count, sampled_posts_count))
    repeatability = eff_above_3x / float(max(sampled_posts_count, 1))

    if cold_start_propensity_lift is None:
        approx_views = int(max(followers_count, 100) * max(median_reach_multiple, 0.5))
        cold_start_propensity_lift = compute_cold_start_propensity_lift(
            followers_count=followers_count,
            views=approx_views,
            likes=None,
            seed_key=seed_key,
        )

    # 4 weighted components summing to 100 max:
    # 1) Median reach-to-follower multiple (0..42)
    reach_term = 42.0 * (1.0 - math.exp(-max(0.0, median_reach_multiple) / 4.2))
    # 2) Repeatability ratio (0..28)
    repeat_term = 28.0 * min(1.0, repeatability / 0.75)
    # 3) 7d follower growth velocity (0..18)
    growth_term = 18.0 * (1.0 - math.exp(-max(0.0, followers_growth_rate) / 0.12))
    # 4) AuthorColdStart Thompson-Sampling / EB quality lift (0..12)
    quality_term = 12.0 * min(1.0, max(0.0, cold_start_propensity_lift) / 0.032)

    raw_score = reach_term + repeat_term + growth_term + quality_term

    # Consistency gate: full credit requires >= 5 sampled original posts AND >= 3 posts >= 3.0x
    if (
        sampled_posts_count >= MIN_SAMPLED_ORIGINAL_POSTS
        and eff_above_3x >= MIN_POSTS_ABOVE_3X
    ):
        consistency_mult = 1.0
    else:
        sample_factor = min(1.0, sampled_posts_count / float(MIN_SAMPLED_ORIGINAL_POSTS))
        hit_factor = min(1.0, eff_above_3x / float(MIN_POSTS_ABOVE_3X))
        consistency_mult = 0.32 + 0.68 * (sample_factor * (hit_factor ** 1.6))

    final_score = min(100.0, max(0.0, raw_score * consistency_mult))
    return round(final_score, 1)


def endorser_authority_multiplier(heavy_hitter_score: float, salsa_scout: float) -> float:
    """Normalized authority + SALSA scout weight: 0.55 * (HH / 100) + 0.45 * salsa_scout."""
    hh_norm = min(max(float(heavy_hitter_score) / 100.0, 0.0), 1.0)
    scout_norm = min(max(float(salsa_scout), 0.0), 1.0)
    return min(max(0.55 * hh_norm + 0.45 * scout_norm, 0.05), 1.0)


def compute_peer_respect_score(
    endorsements: Sequence[EndorsementScoringItem],
) -> tuple[float, dict[int, float]]:
    """Computes Story 4.2 Peer Respect Score (0..100) and per-endorsement weight contributions.

    - Signal weights: follow = 3.0x, quote = 2.0x, reply = 1.0x.
    - Authority scaling: 0.55 * (HH / 100) + 0.45 * salsa_scout.
    - Per-endorser geometric diminishing returns (0.35^k) + distinct-endorser diversity bonus
      so 4 distinct Heavy Hitters decisively beat 4 replies from 1 friend.
    """
    if not endorsements:
        return 0.0, {}

    by_endorser: dict[int, list[tuple[float, int | None]]] = defaultdict(list)
    for item in endorsements:
        sig_w = ENDORSEMENT_SIGNAL_WEIGHTS.get(item.endorsement_type.strip().lower(), 1.0)
        auth_mult = endorser_authority_multiplier(item.heavy_hitter_score, item.salsa_scout)
        raw_w = sig_w * auth_mult
        by_endorser[item.endorser_account_id].append((raw_w, item.endorsement_id))

    contribution_by_id: dict[int, float] = {}
    total_endorser_mass = 0.0

    for _acct_id, items in by_endorser.items():
        items.sort(key=lambda pair: -pair[0])
        acct_mass = 0.0
        for rank_idx, (raw_w, end_id) in enumerate(items):
            effective_w = raw_w * (0.35 ** rank_idx)
            acct_mass += effective_w
            if end_id is not None:
                contribution_by_id[end_id] = round(effective_w, 3)
        total_endorser_mass += acct_mass

    distinct_endorsers = len(by_endorser)
    diversity_bonus = 1.0 + 0.22 * math.log2(float(distinct_endorsers))
    boosted_mass = total_endorser_mass * diversity_bonus

    score = min(100.0, max(0.0, 100.0 * (1.0 - math.exp(-boosted_mass / 3.8))))
    return round(score, 1), contribution_by_id


def compute_combined_rising_score(
    punch_above_weight_score: float,
    peer_respect_score: float,
) -> float:
    """Exact Epic 4 weighted blend: 0.55 * punch_above_weight + 0.45 * peer_respect."""
    return round(0.55 * punch_above_weight_score + 0.45 * peer_respect_score, 1)


def _normalize_projection_weights(raw_weights: dict[str, float]) -> dict[str, float]:
    positive = {slug: float(val) for slug, val in raw_weights.items() if float(val) > 0.0}
    total = sum(positive.values())
    if total <= 0.0:
        return {}
    normalized = {slug: round(val / total, 4) for slug, val in positive.items()}
    # Adjust largest bucket for exact 1.0 sum
    drift = round(1.0 - sum(normalized.values()), 4)
    if normalized and abs(drift) > 0:
        top_slug = max(normalized, key=lambda k: normalized[k])
        normalized[top_slug] = round(max(0.0, normalized[top_slug] + drift), 4)
    return dict(sorted(normalized.items(), key=lambda kv: (-kv[1], kv[0])))


def compute_community_projection(
    communities: Sequence[Community],
    endorsements: Sequence[PeerEndorsement],
    accounts_by_id: dict[int, TrackedAccount],
    memberships_by_account: dict[int, list[AccountCommunityMembership]],
    *,
    fallback_projection: dict[str, float] | None = None,
) -> dict[str, float]:
    """Projects a candidate onto the workspace's K custom communities (summing to 1.0)."""
    if not communities:
        return {}

    comm_by_id = {c.id: c for c in communities}
    valid_slugs = {c.slug for c in communities}
    accum: dict[str, float] = {c.slug: 0.0 for c in communities}

    for end in endorsements:
        endorser = accounts_by_id.get(end.endorser_account_id)
        if endorser is None:
            continue
        sig_w = ENDORSEMENT_SIGNAL_WEIGHTS.get(end.endorsement_type.strip().lower(), 1.0)
        auth_w = endorser_authority_multiplier(endorser.heavy_hitter_score, endorser.salsa_scout)
        memberships = memberships_by_account.get(endorser.id, [])
        for m in memberships:
            if not _is_active_membership(m):
                continue
            comm = comm_by_id.get(m.community_id)
            if comm is None:
                continue
            nmf_blend = 0.60 * float(m.nmf_known_for) + 0.40 * float(m.nmf_interested_in)
            affinity = nmf_blend if nmf_blend > 0.0 else (1.0 if m.is_manual_tag else 0.0)
            if affinity <= 0.0:
                continue
            accum[comm.slug] += sig_w * auth_w * affinity

    projected = _normalize_projection_weights(accum)
    if projected:
        return projected

    if fallback_projection:
        filtered_fallback = {
            slug: float(w)
            for slug, w in fallback_projection.items()
            if slug in valid_slugs and float(w) > 0.0
        }
        norm_fallback = _normalize_projection_weights(filtered_fallback)
        if norm_fallback:
            return norm_fallback

    uniform = 1.0 / float(len(communities))
    return _normalize_projection_weights({c.slug: uniform for c in communities})


def _build_promoted_baseline_curves(
    followers_count: int,
    median_reach_multiple: float,
) -> dict[str, dict[str, int]]:
    est_mult = max(0.25, min(median_reach_multiple * 0.45, 2.5))
    median_48h_views = max(800, int(max(followers_count, 250) * est_mult))
    curves: dict[str, dict[str, int]] = {"p25": {}, "p50": {}, "p75": {}, "p90": {}}
    for age_str, frac in SNAPSHOT_AGE_FRACTIONS.items():
        p50 = max(30, int(median_48h_views * frac))
        curves["p25"][age_str] = max(15, int(p50 * 0.58))
        curves["p50"][age_str] = p50
        curves["p75"][age_str] = int(p50 * 1.55)
        curves["p90"][age_str] = int(p50 * 2.45)
    return curves


async def _harvest_two_hop_interactions_and_fixtures(
    session: AsyncSession,
    workspace: Workspace,
    communities: list[Community],
    tracked_accounts: list[TrackedAccount],
    memberships_by_account: dict[int, list[AccountCommunityMembership]],
    existing_by_handle: dict[str, DiscoveredCandidate],
    tracked_handles: set[str],
    *,
    throttle_mode: str = "normal",
    reference_time: datetime | None = None,
    preloaded_interactions: Sequence[PostInteraction] | None = None,
) -> list[DiscoveredCandidate]:
    """Harvests 2-hop candidates from PostInteractions and provider discovery (batched)."""
    now = _ensure_dt_utc(reference_time) if reference_time is not None else datetime.now(timezone.utc)
    newly_created: list[DiscoveredCandidate] = []
    is_fixture = workspace.x_data_provider == "fixture"
    my_handle = (workspace.my_x_handle or "").strip().lstrip("@").lower()

    # 1. Harvest untracked actors appearing in workspace PostInteractions (all follower tiers)
    if preloaded_interactions is not None:
        interactions = list(preloaded_interactions)
    else:
        interactions = list(
            (
                await session.execute(
                    select(PostInteraction).where(PostInteraction.workspace_id == workspace.id)
                )
            )
            .scalars()
            .all()
        )
    grouped_actors: dict[str, list[PostInteraction]] = defaultdict(list)
    for inter in interactions:
        h_clean = inter.actor_handle.strip().lstrip("@").lower()
        if (
            not h_clean
            or h_clean == my_handle
            or h_clean in tracked_handles
            or inter.actor_followers <= 0
        ):
            continue
        grouped_actors[h_clean].append(inter)

    # Hydrate profiles & sample posts in packed batches (never 1 HTTP call per candidate)
    if grouped_actors:
        new_handles = [h for h in grouped_actors if h not in existing_by_handle]
        hydrated_map: dict[str, Any] = {}
        provider = None
        if new_handles and (is_fixture or throttle_mode != "paused"):
            try:
                provider = get_x_data_provider(workspace.x_data_provider)
                profiles = await provider.get_profiles_by_handles(
                    session, workspace, new_handles
                )
                hydrated_map = {p.handle.strip().lstrip("@").lower(): p for p in profiles if p.handle}
            except (SpendCapExceededError, ProviderNotConfiguredError):
                hydrated_map = {}

        # Batch sample original posts for <50k live candidates in packed <=512-char queries
        live_posts_by_handle: dict[str, list[Any]] = defaultdict(list)
        if not is_fixture and provider is not None and throttle_mode == "normal" and new_handles:
            under_cap_handles = [
                h
                for h in new_handles
                if (
                    hydrated_map[h].followers_count
                    if h in hydrated_map
                    else grouped_actors[h][0].actor_followers
                )
                < COLD_START_FOLLOWER_CAP
            ]
            if under_cap_handles:
                try:
                    batched_posts = await provider.search_recent_posts(
                        session,
                        workspace,
                        handles=under_cap_handles,
                        exclude_replies=True,
                    )
                    for lp in batched_posts:
                        ah = (lp.author_handle or "").strip().lstrip("@").lower()
                        if ah:
                            live_posts_by_handle[ah].append(lp)
                except (SpendCapExceededError, ProviderNotConfiguredError):
                    pass

        for h_clean, actor_inters in grouped_actors.items():
            if h_clean in existing_by_handle:
                continue
            prof = hydrated_map.get(h_clean)
            foll = prof.followers_count if prof else actor_inters[0].actor_followers
            foll_out = int(prof.following_count) if prof else 0
            avatar = prof.avatar_url if prof else None
            is_under_cap = foll < COLD_START_FOLLOWER_CAP

            if is_fixture and is_under_cap:
                digest = hashlib.blake2b(h_clean.encode("utf-8"), digest_size=4).digest()
                post_suffix = int.from_bytes(digest, "little") % 1_000_000
                growth_rate = 0.11
                sampled_cnt = 6
                above_3x_cnt = 4
                med_mult = 4.4
                top_posts: list[dict[str, Any]] = [
                    {
                        "x_post_id": f"9900{post_suffix:06d}",
                        "text": f"Technical breakdown from @{h_clean} resonating across your network.",
                        "views": int(max(foll, 200) * 4.4),
                        "reach_multiple": 4.4,
                    }
                ]
            else:
                growth_rate = 0.0
                sampled_cnt = 0
                above_3x_cnt = 0
                med_mult = 0.0
                top_posts = []
                cand_live_posts = live_posts_by_handle.get(h_clean, [])
                if is_under_cap and cand_live_posts:
                    sample_items = [
                        CandidateSamplePost(
                            x_post_id=lp.x_post_id,
                            text=lp.text,
                            views=lp.views,
                            likes=lp.likes,
                            replies=lp.replies,
                            quotes=lp.quotes,
                            reposts=lp.reposts,
                            bookmarks=lp.bookmarks,
                            is_reply=lp.is_reply,
                            is_retweet=lp.is_retweet,
                            is_quote=lp.is_quote,
                        )
                        for lp in cand_live_posts
                    ]
                    eval_summary = evaluate_sampled_posts(
                        sample_items,
                        foll,
                        seed_key=f"cand:{h_clean}",
                    )
                    sampled_cnt = eval_summary.sampled_posts_count
                    above_3x_cnt = eval_summary.posts_above_3x_count
                    med_mult = eval_summary.median_reach_multiple
                    top_posts = eval_summary.top_posts_json

            cand = DiscoveredCandidate(
                workspace_id=workspace.id,
                x_user_id=(prof.x_user_id if prof else actor_inters[0].actor_x_user_id),
                handle=h_clean,
                display_name=(prof.display_name if prof else h_clean.replace("_", " ").title()),
                bio=(prof.bio if prof else ""),
                avatar_url=avatar,
                followers_count=foll,
                following_count=foll_out,
                followers_growth_rate=growth_rate,
                sampled_posts_count=sampled_cnt,
                posts_above_3x_count=above_3x_cnt,
                median_reach_multiple=med_mult,
                community_projection_json={},
                top_posts_json=top_posts,
                status=("active" if is_under_cap else "over_cap"),
            )
            session.add(cand)
            existing_by_handle[h_clean] = cand
            newly_created.append(cand)

        if newly_created:
            await session.flush()

    # 2. In Fixture Mode, ensure any seeded active candidates missing 2-hop endorsements/posts
    # (such as Workspace 2's K=4 seeded candidates) receive deterministic 2-hop co-signs
    if is_fixture and tracked_accounts:
        comm_slug_by_id = {c.id: c.slug for c in communities}
        accounts_by_slug: dict[str, list[TrackedAccount]] = defaultdict(list)
        for acct in tracked_accounts:
            for m in memberships_by_account.get(acct.id, []):
                if not _is_active_membership(m):
                    continue
                slug = comm_slug_by_id.get(m.community_id)
                if slug:
                    accounts_by_slug[slug].append(acct)

        cands_with_ends = {
            int(cid_val)
            for cid_val in (
                await session.execute(
                    select(PeerEndorsement.candidate_id)
                    .where(PeerEndorsement.workspace_id == workspace.id)
                    .distinct()
                )
            )
            .scalars()
            .all()
        }

        for idx, cand in enumerate(existing_by_handle.values()):
            if cand.status != "active" or cand.followers_count >= COLD_START_FOLLOWER_CAP:
                continue
            if cand.id not in cands_with_ends:
                target_slugs = list((cand.community_projection_json or {}).keys())
                pool: list[TrackedAccount] = []
                for s in target_slugs:
                    pool.extend(accounts_by_slug.get(s, []))
                if not pool:
                    pool = list(tracked_accounts)
                seen_ids: set[int] = set()
                unique_pool: list[TrackedAccount] = []
                for a in pool:
                    if a.id not in seen_ids:
                        seen_ids.add(a.id)
                        unique_pool.append(a)

                for e_off, e_type in ((0, "follow"), (1, "quote"), (2, "reply")):
                    endorser = unique_pool[(idx * 2 + e_off) % len(unique_pool)]
                    session.add(
                        PeerEndorsement(
                            workspace_id=workspace.id,
                            candidate_id=cand.id,
                            endorser_account_id=endorser.id,
                            endorsement_type=e_type,
                            source_x_post_id=(
                                f"199630{cand.id:03d}{e_off}" if e_type != "follow" else None
                            ),
                            weight_contribution=1.0,
                            occurred_at=now - timedelta(hours=6 + idx * 3 + e_off),
                        )
                    )
            if not cand.top_posts_json and cand.median_reach_multiple > 0:
                cand.top_posts_json = [
                    {
                        "x_post_id": f"199610{cand.id:04d}",
                        "text": (
                            f"Production architecture notes from @{cand.handle} hitting "
                            f"{cand.median_reach_multiple}x organic reach."
                        ),
                        "views": int(max(cand.followers_count, 200) * cand.median_reach_multiple),
                        "reach_multiple": cand.median_reach_multiple,
                    }
                ]
        await session.flush()

    return newly_created


def _has_conversation_since(
    endorsements: Sequence[PeerEndorsement], since: datetime | None
) -> bool:
    """True when a reply/quote endorsement occurred after `since` (any, if `since` is unknown)."""
    since_utc = _ensure_dt_utc(since) if since is not None else None
    return any(
        e.endorsement_type.strip().lower() in ("reply", "quote")
        and (since_utc is None or _ensure_dt_utc(e.occurred_at) > since_utc)
        for e in endorsements
    )


def _interaction_weight_for_type(etype: str, is_mutual_endorser: bool) -> float:
    kind = etype.strip().lower()
    if kind == "reply":
        return MUTUAL_REPLY_WEIGHT if is_mutual_endorser else REPLY_WEIGHT
    if kind == "quote":
        return QUOTE_WEIGHT
    if kind in ("repost", "retweet"):
        return REPOST_WEIGHT
    return 1.0


@dataclass(slots=True)
class PrecomputedDiscoveryContext:
    """In-memory graph & ORM state passed from `auto_sync_workspace_network` so Rising candidate
    scoring neither reloads workspace tables nor re-runs `build_analytics_from_orm`."""

    workspace: Workspace
    communities: list[Community]
    all_ws_accounts: list[TrackedAccount]
    memberships: list[AccountCommunityMembership]
    posts: list[Post]
    interactions: list[PostInteraction]
    outcomes_by_handle: dict[str, list[ReplyOutcomeSample]]
    analytics: Any


@dataclass(slots=True)
class RisingRefreshBundle:
    active_scored: list[DiscoveredCandidate]
    all_candidates: list[DiscoveredCandidate]
    all_endorsements: list[PeerEndorsement]
    core_ctx: Any
    communities: list[Community]
    all_ws_accounts: list[TrackedAccount]
    posts: list[Post]
    interactions: list[PostInteraction]
    outcomes_by_handle: dict[str, list[ReplyOutcomeSample]]


async def _refresh_rising_candidates_impl(
    session: AsyncSession,
    workspace_id: int,
    *,
    harvest_from_provider: bool = True,
    throttle_mode: str = "normal",
    commit: bool = True,
    reference_time: datetime | None = None,
    precomputed: PrecomputedDiscoveryContext | None = None,
) -> RisingRefreshBundle:
    now = _ensure_dt_utc(reference_time) if reference_time is not None else datetime.now(timezone.utc)
    if precomputed is not None:
        workspace = precomputed.workspace
        communities = precomputed.communities
        all_ws_accounts = precomputed.all_ws_accounts
        memberships = precomputed.memberships
    else:
        workspace = await session.get(Workspace, workspace_id)
        if workspace is None:
            raise ValueError(f"Workspace {workspace_id} not found.")

        communities = list(
            (
                await session.execute(
                    select(Community)
                    .where(Community.workspace_id == workspace_id)
                    .order_by(Community.sort_order.asc(), Community.id.asc())
                )
            )
            .scalars()
            .all()
        )
        all_ws_accounts = list(
            (
                await session.execute(
                    select(TrackedAccount).where(
                        TrackedAccount.workspace_id == workspace_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        memberships = list(
            (
                await session.execute(
                    select(AccountCommunityMembership).where(
                        AccountCommunityMembership.workspace_id == workspace_id
                    )
                )
            )
            .scalars()
            .all()
        )

    tracked_accounts = [
        a for a in all_ws_accounts if bool(a.is_tracked) and not bool(a.is_muted)
    ]
    muted_handles = {a.handle.strip().lower() for a in all_ws_accounts if bool(a.is_muted)}
    accounts_by_id = {a.id: a for a in tracked_accounts}
    tracked_handles = {a.handle.strip().lower() for a in tracked_accounts}

    memberships_by_account: dict[int, list[AccountCommunityMembership]] = defaultdict(list)
    for m in memberships:
        if m.account_id in accounts_by_id and _is_active_membership(m):
            memberships_by_account[m.account_id].append(m)

    candidates = list(
        (
            await session.execute(
                select(DiscoveredCandidate).where(
                    DiscoveredCandidate.workspace_id == workspace_id
                )
            )
        )
        .scalars()
        .all()
    )
    existing_by_handle = {c.handle.strip().lower(): c for c in candidates}

    if harvest_from_provider:
        new_cands = await _harvest_two_hop_interactions_and_fixtures(
            session,
            workspace,
            communities,
            tracked_accounts,
            memberships_by_account,
            existing_by_handle,
            tracked_handles | muted_handles,
            throttle_mode=throttle_mode,
            reference_time=now,
            preloaded_interactions=(
                precomputed.interactions if precomputed is not None else None
            ),
        )
        candidates.extend(new_cands)

    # Load or reuse workspace reply outcomes so candidate yield & learned core C_yield reflect observed reach
    latest_reply_ts_by_handle: dict[str, datetime] = {}
    all_outcome_samples: list[ReplyOutcomeSample] = []
    if precomputed is not None:
        outcomes_by_handle = precomputed.outcomes_by_handle
        for h_clean, samples in outcomes_by_handle.items():
            for sample in samples:
                all_outcome_samples.append(sample)
                ts_o = _ensure_dt_utc(sample.created_at_x)
                prev_ts = latest_reply_ts_by_handle.get(h_clean)
                if prev_ts is None or ts_o > prev_ts:
                    latest_reply_ts_by_handle[h_clean] = ts_o
    else:
        outcome_rows = list(
            (
                await session.execute(
                    select(OwnReplyOutcome).where(OwnReplyOutcome.workspace_id == workspace_id)
                )
            )
            .scalars()
            .all()
        )
        outcomes_by_handle = defaultdict(list)
        for o in outcome_rows:
            h_clean = (o.target_handle or "").strip().lstrip("@").lower()
            if not h_clean:
                continue
            ts_o = _ensure_dt_utc(o.created_at_x) if o.created_at_x is not None else now
            sample = ReplyOutcomeSample(
                x_reply_post_id=o.x_reply_post_id,
                target_handle=h_clean,
                created_at_x=ts_o,
                views=int(o.latest_views or 0),
                attributed_follows_gained=float(o.attributed_follows_gained or 0.0),
                action_kind=o.action_kind,
                was_mutual_at_reply=bool(o.was_mutual_at_reply),
            )
            outcomes_by_handle[h_clean].append(sample)
            all_outcome_samples.append(sample)
            prev_ts = latest_reply_ts_by_handle.get(h_clean)
            if prev_ts is None or ts_o > prev_ts:
                latest_reply_ts_by_handle[h_clean] = ts_o
    workspace_ref_views = compute_workspace_reference_reply_views(all_outcome_samples)

    # Reuse precomputed analytics when called inside `auto_sync_workspace_network`; otherwise build once
    if precomputed is not None:
        analytics = precomputed.analytics
        accts_loaded = tracked_accounts
        posts_loaded = precomputed.posts
        inters_loaded = precomputed.interactions
    else:
        from tweettracker.graph.service import build_analytics_from_orm, load_workspace_graph_inputs

        (
            ws_loaded,
            comms_loaded,
            accts_loaded,
            mems_loaded,
            edges_loaded,
            posts_loaded,
            snaps_loaded,
            inters_loaded,
        ) = await load_workspace_graph_inputs(session, workspace_id)
        analytics = build_analytics_from_orm(
            ws_loaded,
            comms_loaded,
            accts_loaded,
            mems_loaded,
            edges_loaded,
            posts_loaded,
            snaps_loaded,
            inters_loaded,
            reply_outcomes_by_handle=outcomes_by_handle,
            reference_time=now,
        )
    from tweettracker.graph.builder import AccountNodeInput

    node_inputs = [
        AccountNodeInput(
            id=a.id,
            handle=a.handle,
            display_name=a.display_name,
            followers_count=a.followers_count,
            following_count=a.following_count,
            follows_me=a.follows_me,
            followed_by_me=a.followed_by_me,
            is_auto_tracked=bool(a.is_auto_tracked),
            auto_tracked_at=a.auto_tracked_at,
            created_at=a.created_at,
            yield_score=analytics.simclusters.yield_scores.get(a.id),
            reply_sample_count=(
                analytics.yield_by_account[a.id].reply_sample_count
                if a.id in analytics.yield_by_account
                else int(a.reply_sample_count or 0)
            ),
        )
        for a in accts_loaded
    ]
    cred_map = {u: m.user_cred_v2 for u, m in analytics.heavy_hitters.by_account.items()}
    core_ctx = build_core_graph_context(
        node_inputs,
        my_x_handle=workspace.my_x_handle,
        auto_cluster_ids=analytics.simclusters.auto_cluster_ids,
        primary_community=analytics.simclusters.primary_community,
        pr_by_node=analytics.heavy_hitters.pr_by_account,
        out_deg_by_node=analytics.heavy_hitters.out_deg_by_account,
        cred_by_node=cred_map,
        reference_time=now,
    )
    post_author_by_id = {p.id: p.author_account_id for p in posts_loaded}
    outbound_inter_by_handle: dict[str, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    for pi in inters_loaded:
        ah = (pi.actor_handle or "").strip().lstrip("@").lower()
        target_author_id = post_author_by_id.get(pi.post_id)
        if not ah or target_author_id is None or target_author_id not in core_ctx.core_ids:
            continue
        bw = _interaction_weight_for_type(pi.interaction_type, False)
        dec = decay_multiplier(pi.occurred_at, now)
        outbound_inter_by_handle[ah][target_author_id] += bw * dec

    endorsement_rows = list(
        (
            await session.execute(
                select(PeerEndorsement).where(PeerEndorsement.workspace_id == workspace_id)
            )
        )
        .scalars()
        .all()
    )
    endorsements_by_candidate: dict[int, list[PeerEndorsement]] = defaultdict(list)
    for end in endorsement_rows:
        if end.endorser_account_id in accounts_by_id:
            endorsements_by_candidate[end.candidate_id].append(end)

    has_own_h = bool(workspace.my_x_handle and workspace.my_x_handle.strip())
    active_scored: list[DiscoveredCandidate] = []

    for cand in candidates:
        cand_h = cand.handle.strip().lstrip("@").lower()
        if cand_h in muted_handles:
            cand.status = "dismissed"
            cand.core_affinity_score = 0.0
            cand.yield_score = 0.0
            continue

        if cand.status == "dismissed":
            continue

        if cand_h in tracked_handles:
            cand.status = "promoted"
            continue

        cand_ends = endorsements_by_candidate.get(cand.id, [])
        scoring_items = [
            EndorsementScoringItem(
                endorsement_id=e.id,
                endorser_account_id=e.endorser_account_id,
                endorsement_type=e.endorsement_type,
                heavy_hitter_score=accounts_by_id[e.endorser_account_id].heavy_hitter_score,
                salsa_scout=accounts_by_id[e.endorser_account_id].salsa_scout,
            )
            for e in cand_ends
            if e.endorser_account_id in accounts_by_id
        ]

        peer_resp, contrib_map = compute_peer_respect_score(scoring_items)
        for e in cand_ends:
            if e.id in contrib_map:
                e.weight_contribution = contrib_map[e.id]

        proj = compute_community_projection(
            communities=communities,
            endorsements=cand_ends,
            accounts_by_id=accounts_by_id,
            memberships_by_account=memberships_by_account,
            fallback_projection=cand.community_projection_json,
        )
        cand.peer_respect_score = peer_resp
        cand.community_projection_json = proj

        # Compute SimClusters Core Affinity + virtual UserCredV2 across ALL follower sizes (zero size cliffs)
        in_conv: dict[int, float] = defaultdict(float)
        in_foll: dict[int, float] = defaultdict(float)
        first_follow_at: datetime | None = None
        for e in cand_ends:
            end_acct = accounts_by_id.get(e.endorser_account_id)
            if end_acct is None:
                continue
            etype = e.endorsement_type.strip().lower()
            if etype == "follow":
                in_foll[end_acct.id] = max(in_foll[end_acct.id], FOLLOW_WEIGHT)
                occ = _ensure_dt_utc(e.occurred_at)
                if first_follow_at is None or occ < first_follow_at:
                    first_follow_at = occ
            else:
                is_mut_end = has_own_h and bool(end_acct.follows_me and end_acct.followed_by_me)
                bw = _interaction_weight_for_type(etype, is_mut_end)
                dec = decay_multiplier(e.occurred_at, now)
                in_conv[end_acct.id] += bw * dec

        out_conv = dict(outbound_inter_by_handle.get(cand_h, {}))
        cand.core_affinity_score = score_behavioral_core_affinity(
            core_ctx,
            node_id=-abs(int(cand.id or 1)),
            inbound_conv=dict(in_conv),
            outbound_conv=out_conv,
            inbound_follow=dict(in_foll),
            outbound_follow={},
            aud_cos_bonus=0.0,
            known_cred_v2=None,
            is_auto_tracked=True,
            # Decay co-follow-only evidence from when it first appeared, exactly as tracked
            # accounts decay from auto_tracked_at; otherwise a pruned account re-qualifies
            # as an undecayed candidate on the next sync and the list never settles.
            auto_tracked_at=first_follow_at,
            is_self_core_anchor=False,
            reference_time=now,
            followers_count=cand.followers_count,
        )

        has_cand_climb = int(cand.posts_above_3x_count or 0) >= 1
        cand_prior = compute_reach_prior_score(
            core_affinity_score=cand.core_affinity_score,
            followers_count=cand.followers_count,
            has_recent_climbing_post=has_cand_climb,
            is_mutual=False,
            follows_me=False,
        )
        cand_ym = compute_account_yield_metrics(
            prior_score=cand_prior,
            outcomes=outcomes_by_handle.get(cand_h, ()),
            workspace_ref_views=workspace_ref_views,
            is_mutual=False,
            is_auto_tracked=True,
            created_or_added_at=cand.updated_at,
            has_recent_climbing_post=has_cand_climb,
            reference_time=now,
        )
        cand.yield_score = cand_ym.yield_score
        cand.reply_sample_count = cand_ym.reply_sample_count
        cand.avg_reply_views = cand_ym.avg_reply_views
        cand.attributed_follows_30d = cand_ym.attributed_follows_30d

        latest_own_reply = latest_reply_ts_by_handle.get(cand_h)
        has_new_own_reply = latest_own_reply is not None and (
            cand.pruned_at is None or latest_own_reply > _ensure_dt_utc(cand.pruned_at)
        )

        # A pruned account stays out until new conversational/reply evidence arrives after the prune
        if (
            cand.status == "pruned"
            and not _has_conversation_since(cand_ends, cand.pruned_at)
            and not has_new_own_reply
        ):
            continue

        # Enforce ColdStartFollowerCap = 50,000 (<50k strictly) for the Epic 4 Rising leaderboard ONLY
        if cand.followers_count >= COLD_START_FOLLOWER_CAP:
            cand.punch_above_weight_score = 0.0
            cand.combined_rising_score = 0.0
            if cand.status in ("active", "promoted", "pruned"):
                cand.status = "over_cap"
            continue

        if cand.status in ("over_cap", "promoted", "pruned"):
            cand.status = "active"

        paw = compute_punch_above_weight_score(
            followers_count=cand.followers_count,
            sampled_posts_count=cand.sampled_posts_count,
            posts_above_3x_count=cand.posts_above_3x_count,
            median_reach_multiple=cand.median_reach_multiple,
            followers_growth_rate=cand.followers_growth_rate,
            seed_key=f"cand:{cand.handle}",
        )
        cand.punch_above_weight_score = paw
        cand.combined_rising_score = compute_combined_rising_score(paw, peer_resp)
        if cand.status == "active":
            active_scored.append(cand)

    if commit:
        await session.commit()
    else:
        await session.flush()
    active_scored.sort(key=lambda c: (-c.combined_rising_score, -c.punch_above_weight_score))
    return RisingRefreshBundle(
        active_scored=active_scored,
        all_candidates=candidates,
        all_endorsements=endorsement_rows,
        core_ctx=core_ctx,
        communities=communities,
        all_ws_accounts=all_ws_accounts,
        posts=list(posts_loaded),
        interactions=list(inters_loaded),
        outcomes_by_handle=dict(outcomes_by_handle),
    )


async def refresh_rising_candidates(
    session: AsyncSession,
    workspace_id: int,
    *,
    harvest_from_provider: bool = True,
    throttle_mode: str = "normal",
    commit: bool = True,
    reference_time: datetime | None = None,
) -> list[DiscoveredCandidate]:
    """Recomputes Punch-Above-Weight, SALSA Peer Respect, SimClusters Core Affinity, and K-community projections."""
    bundle = await _refresh_rising_candidates_impl(
        session,
        workspace_id,
        harvest_from_provider=harvest_from_provider,
        throttle_mode=throttle_mode,
        commit=commit,
        reference_time=reference_time,
    )
    return bundle.active_scored


CORE_AFFINITY_ADD_THRESHOLD: float = 0.55
CORE_AFFINITY_THROTTLED_ADD_THRESHOLD: float = 0.63
CORE_AFFINITY_PRUNE_THRESHOLD: float = 0.32
# Minimum similarity to the network for a candidate to be considered at all (relevance floor)
RELEVANCE_FLOOR: float = 0.30
AUTO_PRUNE_INACTIVE_DAYS: int = 30
STALE_FOLLOW_CANDIDATE_DAYS: int = 7
STALE_CANDIDATE_RETENTION_DAYS: int = 14
ENDORSEMENT_RETENTION_DAYS: int = 30


def _ensure_dt_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


async def promote_candidate_to_community(
    session: AsyncSession,
    workspace_id: int,
    candidate_id: int,
    community_ids: Sequence[int] | None = None,
    *,
    is_tracked: bool = True,
    is_auto_tracked: bool = False,
    auto_tracked_at: datetime | None = None,
    commit: bool = True,
) -> TrackedAccount:
    """Promotes a DiscoveredCandidate into TrackedAccount + Community + RelationshipEdges."""
    cand = (
        await session.execute(
            select(DiscoveredCandidate).where(
                DiscoveredCandidate.id == candidate_id,
                DiscoveredCandidate.workspace_id == workspace_id,
            )
        )
    ).scalar_one_or_none()
    if cand is None:
        raise LookupError(f"Candidate {candidate_id} not found in workspace {workspace_id}.")

    ws_communities = list(
        (
            await session.execute(
                select(Community)
                .where(Community.workspace_id == workspace_id)
                .order_by(Community.sort_order.asc(), Community.id.asc())
            )
        )
        .scalars()
        .all()
    )
    comm_by_id = {c.id: c for c in ws_communities}
    comm_by_slug = {c.slug: c for c in ws_communities}

    requested_ids = [int(cid) for cid in (community_ids or []) if int(cid) in comm_by_id]
    if community_ids and not requested_ids:
        raise ValueError("None of the supplied community_ids belong to this workspace.")

    if not requested_ids and ws_communities:
        proj = cand.community_projection_json or {}
        ranked_slugs = sorted(proj.keys(), key=lambda s: -float(proj.get(s, 0.0)))
        for slug in ranked_slugs:
            if slug in comm_by_slug:
                requested_ids.append(comm_by_slug[slug].id)
                break
        if not requested_ids:
            requested_ids.append(ws_communities[0].id)

    clean_handle = cand.handle.strip().lstrip("@").lower()
    acct = (
        await session.execute(
            select(TrackedAccount).where(
                TrackedAccount.workspace_id == workspace_id,
                TrackedAccount.handle == clean_handle,
            )
        )
    ).scalar_one_or_none()

    try:
        numeric_uid = int(cand.x_user_id)
    except ValueError:
        digest = hashlib.blake2b(clean_handle.encode("utf-8"), digest_size=4).digest()
        numeric_uid = int.from_bytes(digest, "little") % 100_000_000

    mass = max(1e-11, (max(cand.followers_count, 100) / 2_500_000.0) * 1.15)
    u_cred = round(UserCredV2.from_mass(numeric_uid, mass).score, 1)
    foll_7d = max(
        50,
        int(round(cand.followers_count / (1.0 + max(0.0, cand.followers_growth_rate)))),
    )
    baseline_curves = _build_promoted_baseline_curves(
        cand.followers_count, cand.median_reach_multiple
    )

    n_circles = len(set(requested_ids))
    bridge_role = (
        "multi_circle_connector"
        if n_circles >= 3
        else ("two_way_bridge" if n_circles == 2 else "provincial")
    )
    primary_comm_name = (
        comm_by_id[requested_ids[0]].name if requested_ids else "Network Core"
    )
    effective_auto_ts = (
        _ensure_dt_utc(auto_tracked_at)
        if auto_tracked_at is not None
        else datetime.now(timezone.utc)
    )

    if acct is None:
        acct = TrackedAccount(
            workspace_id=workspace_id,
            x_user_id=cand.x_user_id,
            handle=clean_handle,
            display_name=cand.display_name or clean_handle,
            bio=cand.bio,
            avatar_url=cand.avatar_url,
            followers_count=cand.followers_count,
            following_count=max(0, int(cand.following_count or 0)),
            followers_7d_ago=foll_7d,
            is_tracked=is_tracked,
            is_auto_tracked=is_auto_tracked,
            is_muted=False,
            auto_tracked_at=(effective_auto_ts if is_auto_tracked else None),
            relationship_state="none",
            user_cred_v2=u_cred,
            core_affinity_score=float(cand.core_affinity_score or 0.0),
            yield_score=float(cand.yield_score or cand.core_affinity_score or 0.0),
            reply_sample_count=int(cand.reply_sample_count or 0),
            avg_reply_views=float(cand.avg_reply_views or 0.0),
            attributed_follows_30d=float(cand.attributed_follows_30d or 0.0),
            bridge_role=bridge_role,
            sub_pocket_label=f"{primary_comm_name} Core",
            baseline_curves_json=baseline_curves,
        )
        session.add(acct)
        await session.flush()
    else:
        if bool(acct.is_muted) and is_auto_tracked:
            raise ValueError(f"Account @{clean_handle} is muted and cannot be auto-promoted.")
        if not is_auto_tracked:
            acct.is_muted = False
            acct.is_auto_tracked = False
            acct.auto_tracked_at = None
        elif not bool(acct.is_tracked):
            acct.is_auto_tracked = True
            acct.auto_tracked_at = effective_auto_ts
        acct.is_tracked = is_tracked
        acct.followers_count = max(acct.followers_count, cand.followers_count)
        if cand.following_count and cand.following_count > 0:
            acct.following_count = cand.following_count
        if cand.avatar_url and not acct.avatar_url:
            acct.avatar_url = cand.avatar_url
        acct.core_affinity_score = float(cand.core_affinity_score or acct.core_affinity_score or 0.0)
        if cand.yield_score and float(cand.yield_score) > 0.0:
            acct.yield_score = float(cand.yield_score)
            acct.reply_sample_count = int(cand.reply_sample_count or acct.reply_sample_count or 0)
            acct.avg_reply_views = float(cand.avg_reply_views or acct.avg_reply_views or 0.0)
            acct.attributed_follows_30d = float(
                cand.attributed_follows_30d or acct.attributed_follows_30d or 0.0
            )
        acct.bridge_role = bridge_role
        if not acct.baseline_curves_json:
            acct.baseline_curves_json = baseline_curves
        if acct.user_cred_v2 <= 0.0:
            acct.user_cred_v2 = u_cred

    # Attach AccountCommunityMembership rows
    existing_memberships = {
        m.community_id: m
        for m in (
            await session.execute(
                select(AccountCommunityMembership).where(
                    AccountCommunityMembership.account_id == acct.id
                )
            )
        )
        .scalars()
        .all()
    }
    proj = cand.community_projection_json or {}
    for rank_i, cid in enumerate(dict.fromkeys(requested_ids)):
        comm = comm_by_id[cid]
        proj_w = float(proj.get(comm.slug, 0.80 if rank_i == 0 else 0.45))
        if cid not in existing_memberships:
            session.add(
                AccountCommunityMembership(
                    workspace_id=workspace_id,
                    account_id=acct.id,
                    community_id=cid,
                    is_manual_tag=(not is_auto_tracked),
                    nmf_known_for=round(max(0.35, min(0.95, proj_w)), 3),
                    nmf_interested_in=round(max(0.35, min(0.95, proj_w)), 3),
                )
            )
        elif not is_auto_tracked:
            existing_memberships[cid].is_manual_tag = True

    # Convert PeerEndorsements into RelationshipEdges (endorser -> newly promoted account)
    endorsements = list(
        (
            await session.execute(
                select(PeerEndorsement).where(PeerEndorsement.candidate_id == cand.id)
            )
        )
        .scalars()
        .all()
    )
    for end in endorsements:
        if end.endorser_account_id == acct.id:
            continue
        e_type = end.endorsement_type.strip().lower()
        existing_edge = (
            await session.execute(
                select(RelationshipEdge).where(
                    RelationshipEdge.workspace_id == workspace_id,
                    RelationshipEdge.source_account_id == end.endorser_account_id,
                    RelationshipEdge.target_account_id == acct.id,
                    RelationshipEdge.edge_type == e_type,
                )
            )
        ).scalar_one_or_none()
        sig_w = FOLLOW_WEIGHT if e_type == "follow" else _interaction_weight_for_type(e_type, False)
        if existing_edge is None:
            session.add(
                RelationshipEdge(
                    workspace_id=workspace_id,
                    source_account_id=end.endorser_account_id,
                    target_account_id=acct.id,
                    edge_type=e_type,
                    interaction_count=1,
                    decayed_weight=round(max(0.5, sig_w), 3),
                    last_event_at=end.occurred_at,
                )
            )
        else:
            prev_ev = _ensure_dt_utc(existing_edge.last_event_at) if existing_edge.last_event_at else None
            occ_ev = _ensure_dt_utc(end.occurred_at)
            if e_type != "follow" and (prev_ev is None or occ_ev > prev_ev):
                existing_edge.interaction_count += 1
                existing_edge.decayed_weight = round(
                    min(40.0, existing_edge.decayed_weight + max(0.5, sig_w)),
                    3,
                )
            if prev_ev is None or occ_ev >= prev_ev:
                existing_edge.last_event_at = occ_ev

    cand.status = "promoted"
    if commit:
        await session.commit()
        await session.refresh(acct)
    else:
        await session.flush()
    return acct


async def dismiss_candidate(
    session: AsyncSession,
    workspace_id: int,
    candidate_id: int,
    *,
    restore: bool = False,
) -> DiscoveredCandidate:
    """Marks a Rising candidate as dismissed (or restores to active when restore=True)."""
    cand = (
        await session.execute(
            select(DiscoveredCandidate).where(
                DiscoveredCandidate.id == candidate_id,
                DiscoveredCandidate.workspace_id == workspace_id,
            )
        )
    ).scalar_one_or_none()
    if cand is None:
        raise LookupError(f"Candidate {candidate_id} not found in workspace {workspace_id}.")

    cand.status = "active" if restore else "dismissed"
    await session.commit()
    await session.refresh(cand)
    return cand


async def restore_candidate(
    session: AsyncSession,
    workspace_id: int,
    candidate_id: int,
) -> DiscoveredCandidate:
    """Restores a previously dismissed Rising candidate back to the active queue."""
    return await dismiss_candidate(session, workspace_id, candidate_id, restore=True)


async def _record_pruned_candidates(
    session: AsyncSession,
    workspace_id: int,
    handles: Sequence[str],
    now_utc: datetime,
) -> None:
    """Marks pruned/displaced auto-adds as `pruned` candidates (creating the row if missing) so they
    re-qualify only on reply/quote evidence newer than `pruned_at`, never on re-scored follows."""
    pruned_set = {h.strip().lstrip("@").lower() for h in handles if h}
    if not pruned_set:
        return
    cand_by_handle = {
        c.handle.strip().lstrip("@").lower(): c
        for c in (
            await session.execute(
                select(DiscoveredCandidate).where(DiscoveredCandidate.workspace_id == workspace_id)
            )
        )
        .scalars()
        .all()
    }
    pruned_accts = (
        await session.execute(
            select(TrackedAccount).where(TrackedAccount.workspace_id == workspace_id)
        )
    ).scalars().all()
    for acct in pruned_accts:
        h = acct.handle.strip().lstrip("@").lower()
        if h not in pruned_set:
            continue
        cand = cand_by_handle.get(h)
        if cand is None:
            cand = DiscoveredCandidate(
                workspace_id=workspace_id,
                x_user_id=acct.x_user_id,
                handle=h,
                display_name=acct.display_name,
                bio=acct.bio,
                avatar_url=acct.avatar_url,
                followers_count=acct.followers_count,
                following_count=acct.following_count,
            )
            session.add(cand)
        if cand.status != "dismissed":
            cand.status = "pruned"
            cand.pruned_at = now_utc
    await session.flush()


async def cleanup_stale_candidates(
    session: AsyncSession,
    workspace_id: int,
    *,
    candidates: Sequence[DiscoveredCandidate],
    endorsements: Sequence[PeerEndorsement],
    active_account_ids: set[int],
    known_account_handles: set[str],
    outcomes_by_handle: dict[str, list[ReplyOutcomeSample]],
    now_utc: datetime,
) -> tuple[int, int]:
    """Bulk-deletes stale low-affinity candidate staging rows and orphaned/converted endorsements.

    - Preserves `dismissed` rows and `pruned` tombstones permanently. A pruned tombstone is what keeps
      a dropped account out until new conversation arrives; deleting it lets the next crawl re-stage
      the account as a fresh, undecayed candidate and restart the add -> fade -> prune cycle.
    - Preserves `promoted` rows while tracked (keeping candidate IDs stable if later pruned), while
      cleaning up their redundant `PeerEndorsement` rows already copied into `RelationshipEdge`.
    - Deletes unpromoted `active`/`over_cap` candidates below `CORE_AFFINITY_PRUNE_THRESHOLD` (0.32)
      with zero own-reply outcomes and zero sampled >=3x posts once orphaned or past retention
      (7d for follow-only noise, 14d for stale low-affinity mentions).
    """
    follow_cutoff = now_utc - timedelta(days=STALE_FOLLOW_CANDIDATE_DAYS)
    cand_cutoff = now_utc - timedelta(days=STALE_CANDIDATE_RETENTION_DAYS)
    end_cutoff = now_utc - timedelta(days=ENDORSEMENT_RETENTION_DAYS)

    cand_by_id = {c.id: c for c in candidates if c.id is not None}
    ends_by_cand: dict[int, list[PeerEndorsement]] = defaultdict(list)
    stale_end_ids: set[int] = set()

    for end in endorsements:
        if end.id is None:
            continue
        ends_by_cand[end.candidate_id].append(end)
        parent = cand_by_id.get(end.candidate_id)
        etype = end.endorsement_type.strip().lower()
        if (
            parent is None
            or parent.status == "promoted"
            or end.endorser_account_id not in active_account_ids
            or (
                parent.status in ("active", "over_cap")
                and etype in ("reply", "quote")
                and _ensure_dt_utc(end.occurred_at) < end_cutoff
            )
        ):
            stale_end_ids.add(end.id)

    stale_cand_ids: set[int] = set()
    for cand in candidates:
        if cand.id is None or cand.status in ("dismissed", "pruned"):
            continue
        cand_h = cand.handle.strip().lstrip("@").lower()
        if cand.status == "promoted":
            if cand_h not in known_account_handles:
                stale_cand_ids.add(cand.id)
            continue
        if cand.status not in ("active", "over_cap"):
            continue

        if (
            cand_h in outcomes_by_handle
            or int(cand.reply_sample_count or 0) > 0
            or int(cand.posts_above_3x_count or 0) > 0
            or max(float(cand.core_affinity_score or 0.0), float(cand.yield_score or 0.0))
            >= CORE_AFFINITY_PRUNE_THRESHOLD
        ):
            continue

        raw_ends = ends_by_cand.get(cand.id, [])
        surviving_ends = [e for e in raw_ends if e.id not in stale_end_ids]
        updated_ts = _ensure_dt_utc(cand.updated_at) if cand.updated_at is not None else now_utc

        if not surviving_ends:
            if raw_ends or updated_ts <= follow_cutoff:
                stale_cand_ids.add(cand.id)
            continue

        latest_ts = max(_ensure_dt_utc(e.occurred_at) for e in surviving_ends)
        has_conv = any(
            e.endorsement_type.strip().lower() in ("reply", "quote") for e in surviving_ends
        )
        if (not has_conv and latest_ts <= follow_cutoff) or latest_ts <= cand_cutoff:
            stale_cand_ids.add(cand.id)
            for e in surviving_ends:
                if e.id is not None:
                    stale_end_ids.add(e.id)

    if stale_cand_ids:
        for cid in stale_cand_ids:
            for e in ends_by_cand.get(cid, []):
                if e.id is not None:
                    stale_end_ids.add(e.id)

    end_list = sorted(stale_end_ids)
    for idx in range(0, len(end_list), 500):
        chunk = end_list[idx : idx + 500]
        await session.execute(delete(PeerEndorsement).where(PeerEndorsement.id.in_(chunk)))

    cand_list = sorted(stale_cand_ids)
    for idx in range(0, len(cand_list), 500):
        chunk = cand_list[idx : idx + 500]
        await session.execute(delete(DiscoveredCandidate).where(DiscoveredCandidate.id.in_(chunk)))

    return len(cand_list), len(end_list)


async def auto_sync_workspace_network(
    session: AsyncSession,
    workspace_id: int,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Reply-reach network sync:
    1) Paces live crawls against `monthly_unit_budget` (100k default; throttles >=80%, pauses >=95%)
       and syncs `@my_x_handle` replies/quotes + follower snapshot (~1-2 units/sync).
    2) Recomputes `yield_score`, `core_affinity_score`, and `UserCredV2` across tracked accounts.
    3) Prunes on measured reply reach (below a fixed floor, so pruning can't ratchet) for seeds
       and auto-adds alike; unmeasured accounts fall back to the affinity rule (`< 0.32` after 48h).
       `MIN_ACTIVE_CORE_FLOOR = 10` is a safety floor when the network has no healthy core.
    4) Tests the most relevant untested candidates' reply reach, then promotes candidates that clear
       the relevance floor and beat the network's median reply reach (or the owner's proven yield),
       then garbage-collects stale candidate staging rows and orphaned endorsements.
    """
    now_utc = _ensure_dt_utc(now) if now is not None else datetime.now(timezone.utc)
    ws = await session.get(Workspace, workspace_id)
    if ws is None:
        raise ValueError(f"Workspace {workspace_id} not found.")

    ws.last_discovery_sync_at = now_utc

    unit_proj = await estimate_workspace_monthly_units(session, ws, now=now_utc)
    throttle_mode = str(unit_proj.get("discovery_throttle_mode", "normal"))

    profiles_refreshed = 0
    own_replies_synced = 0
    if ws.x_data_provider != "fixture" and throttle_mode != "paused":
        from tweettracker.graph.live_crawler import (
            crawl_live_seed_network,
            refresh_tracked_profiles,
        )

        await crawl_live_seed_network(
            session,
            ws,
            throttle_mode=throttle_mode,
            now=now_utc,
        )

        own_yield_res = await sync_own_reply_yield(
            session,
            ws,
            throttle_mode=throttle_mode,
            now=now_utc,
        )
        own_replies_synced = int(own_yield_res.get("replies_upserted", 0)) + int(
            own_yield_res.get("replies_rehydrated", 0)
        )

        if throttle_mode == "normal":
            profiles_refreshed = await refresh_tracked_profiles(session, ws)

    from tweettracker.graph.service import (
        recompute_workspace_graph,
        recompute_workspace_graph_with_context,
    )

    recomp = await recompute_workspace_graph_with_context(
        session, workspace_id, commit=False, reference_time=now_utc
    )

    all_ws_accounts = list(
        (
            await session.execute(
                select(TrackedAccount).where(TrackedAccount.workspace_id == workspace_id)
            )
        )
        .scalars()
        .all()
    )

    # 1. Prune: measured reply reach first; affinity for unmeasured accounts (48h grace) and a
    #    30-day inactivity sweep. Seeds get no protection period.
    auto_grace_cutoff = now_utc - timedelta(hours=BOOTSTRAP_GRACE_HOURS)
    cutoff_30d = now_utc - timedelta(days=AUTO_PRUNE_INACTIVE_DAYS)

    def _acct_eff_score(a: TrackedAccount) -> float:
        if int(a.reply_sample_count or 0) > 0 or float(a.yield_score or 0.0) > 0.0:
            return float(a.yield_score or 0.0)
        return float(a.core_affinity_score or 0.0)

    from tweettracker.graph.reply_reach import (
        THROTTLED_ADD_MARGIN,
        is_measured,
        reach_thresholds,
        recompute_reach_estimates,
        test_candidates,
    )

    reach_add_threshold, reach_prune_threshold = reach_thresholds(
        [float(a.reply_reach_score) for a in recomp.accounts if is_measured(a)]
    )

    active_accts_for_prune = sorted(
        list(recomp.accounts),
        key=lambda a: (
            float(a.reply_reach_score) if is_measured(a) else _acct_eff_score(a),
            float(a.core_affinity_score or 0.0),
            a.id,
        ),
    )

    active_remaining = len(active_accts_for_prune)
    # With a healthy core (3+ accounts clearly worth keeping) the network can shrink freely;
    # without one, keep MIN_ACTIVE_CORE_FLOOR accounts so an unattended workspace never empties.
    healthy_active_count = sum(
        1
        for a in active_accts_for_prune
        if (
            float(a.reply_reach_score) >= reach_add_threshold
            if is_measured(a)
            else _acct_eff_score(a) >= 0.45
        )
    )
    prune_floor = 1 if healthy_active_count >= 3 else MIN_ACTIVE_CORE_FLOOR
    auto_pruned_handles: list[str] = []
    still_tracked_30d: list[TrackedAccount] = []

    for acct in active_accts_for_prune:
        owner_proven = int(acct.reply_sample_count or 0) >= 2 and float(acct.yield_score or 0.0) >= 0.55
        if is_measured(acct):
            # Measured reply reach decides, for seeds and auto-adds alike
            if (
                float(acct.reply_reach_score) < reach_prune_threshold
                and not owner_proven
                and active_remaining > prune_floor
            ):
                acct.is_tracked = False
                auto_pruned_handles.append(acct.handle)
                active_remaining -= 1
            continue
        # Not measured yet: the earlier affinity rule, with no extra protection for seeds
        added_at = acct.auto_tracked_at if bool(acct.is_auto_tracked) else acct.created_at
        added_ts = _ensure_dt_utc(added_at) if added_at is not None else now_utc
        if (
            added_ts <= auto_grace_cutoff
            and _acct_eff_score(acct) < CORE_AFFINITY_PRUNE_THRESHOLD
            and not owner_proven
            and active_remaining > prune_floor
        ):
            acct.is_tracked = False
            auto_pruned_handles.append(acct.handle)
            active_remaining -= 1
            continue
        if added_ts <= cutoff_30d:
            still_tracked_30d.append(acct)

    if still_tracked_30d:
        cand_ids_set = {a.id for a in still_tracked_30d}
        posts_rows = [p for p in recomp.posts if p.author_account_id in cand_ids_set]
        recent_strong_post_authors = {
            p.author_account_id
            for p in posts_rows
            if _ensure_dt_utc(p.created_at_x) >= cutoff_30d
            and (float(p.breakout_score) >= 50.0 or int(p.latest_views) >= 500)
        }
        author_post_ids = {p.id: p.author_account_id for p in posts_rows}

        recent_inter_accounts: set[int] = set()
        for inter in recomp.interactions:
            if _ensure_dt_utc(inter.occurred_at) < cutoff_30d:
                continue
            if inter.actor_account_id in cand_ids_set:
                recent_inter_accounts.add(inter.actor_account_id)
            if inter.post_id in author_post_ids:
                recent_inter_accounts.add(author_post_ids[inter.post_id])

        recent_edge_accounts: set[int] = set()
        for edge in recomp.relationship_edges:
            if edge.last_event_at is None or _ensure_dt_utc(edge.last_event_at) < cutoff_30d:
                continue
            if edge.source_account_id in cand_ids_set:
                recent_edge_accounts.add(edge.source_account_id)
            if edge.target_account_id in cand_ids_set:
                recent_edge_accounts.add(edge.target_account_id)

        for acct in still_tracked_30d:
            if (
                acct.id in recent_strong_post_authors
                or acct.id in recent_inter_accounts
                or acct.id in recent_edge_accounts
                or (int(acct.reply_sample_count or 0) > 0 and _acct_eff_score(acct) >= 0.50)
                or active_remaining <= prune_floor
            ):
                continue
            acct.is_tracked = False
            auto_pruned_handles.append(acct.handle)
            active_remaining -= 1

    if auto_pruned_handles:
        await _record_pruned_candidates(session, workspace_id, auto_pruned_handles, now_utc)

    # 2. Score all untracked candidates reusing `recomp.analytics` (no duplicate graph rebuild)
    precomp = PrecomputedDiscoveryContext(
        workspace=ws,
        communities=recomp.communities,
        all_ws_accounts=all_ws_accounts,
        memberships=recomp.memberships,
        posts=recomp.posts,
        interactions=recomp.interactions,
        outcomes_by_handle=recomp.outcomes_by_handle,
        analytics=recomp.analytics,
    )
    refresh_bundle = await _refresh_rising_candidates_impl(
        session,
        workspace_id,
        harvest_from_provider=(throttle_mode != "paused"),
        throttle_mode=throttle_mode,
        commit=False,
        reference_time=now_utc,
        precomputed=precomp,
    )

    comm_by_slug = {c.slug: c for c in refresh_bundle.communities}
    muted_handles = {
        a.handle.strip().lower() for a in refresh_bundle.all_ws_accounts if bool(a.is_muted)
    }
    active_tracked_accts = [
        a for a in refresh_bundle.all_ws_accounts if bool(a.is_tracked) and not bool(a.is_muted)
    ]
    already_tracked_handles = {a.handle.strip().lower() for a in active_tracked_accts}
    has_own_h = bool(ws.my_x_handle and ws.my_x_handle.strip())

    core_ids = set(refresh_bundle.core_ctx.core_ids)
    mutual_core_ids = {
        a.id
        for a in active_tracked_accts
        if has_own_h and bool(a.follows_me and a.followed_by_me)
    }

    effective_add_threshold = (
        CORE_AFFINITY_THROTTLED_ADD_THRESHOLD
        if throttle_mode == "throttled"
        else CORE_AFFINITY_ADD_THRESHOLD
    )

    auto_added_handles: list[str] = []
    newly_promoted_accts: list[TrackedAccount] = []

    if throttle_mode != "paused":
        all_cands = [
            c for c in refresh_bundle.all_candidates if c.status in ("active", "over_cap")
        ]
        core_ties_by_cand: dict[int, set[int]] = defaultdict(set)
        has_mutual_conv_by_cand: dict[int, bool] = defaultdict(bool)
        for end in refresh_bundle.all_endorsements:
            if end.endorser_account_id in core_ids:
                core_ties_by_cand[end.candidate_id].add(end.endorser_account_id)
                if (
                    end.endorser_account_id in mutual_core_ids
                    and end.endorsement_type.strip().lower() in ("reply", "quote")
                ):
                    has_mutual_conv_by_cand[end.candidate_id] = True

        posts_author_map = {p.id: p.author_account_id for p in refresh_bundle.posts}
        cand_id_by_handle = {c.handle.strip().lower(): c.id for c in all_cands}
        for inter in refresh_bundle.interactions:
            ah = (inter.actor_handle or "").strip().lstrip("@").lower()
            cid_match = cand_id_by_handle.get(ah)
            p_author = posts_author_map.get(inter.post_id)
            if cid_match is not None and p_author in core_ids:
                core_ties_by_cand[cid_match].add(int(p_author))

        pruned_this_sync = {h.strip().lower() for h in auto_pruned_handles}

        def _is_relevant(c: DiscoveredCandidate) -> bool:
            # Similarity to the network is the relevance floor, not the admission rule
            return (
                float(c.core_affinity_score or 0.0) >= RELEVANCE_FLOOR
                or len(core_ties_by_cand.get(c.id, set())) >= 2
                or has_mutual_conv_by_cand.get(c.id, False)
                or int(c.reply_sample_count or 0) >= 1
            )

        def _eligible(c: DiscoveredCandidate) -> bool:
            h = c.handle.strip().lower()
            return (
                h not in muted_handles
                and h not in already_tracked_handles
                and h not in pruned_this_sync
            )

        relevant_cands = sorted(
            (c for c in all_cands if _is_relevant(c) and _eligible(c)),
            key=lambda c: (-float(c.core_affinity_score or 0.0), c.id),
        )
        # Measure reply reach on the most relevant untested candidates before deciding
        if await test_candidates(
            session, ws, relevant_cands, now=now_utc, throttle_mode=throttle_mode
        ):
            await recompute_reach_estimates(session, ws, now=now_utc)

        cand_add_threshold = reach_add_threshold + (
            THROTTLED_ADD_MARGIN if throttle_mode == "throttled" else 0.0
        )

        def _cand_qualifying_score(c: DiscoveredCandidate) -> float:
            if is_measured(c):
                return float(c.reply_reach_score)
            return float(c.yield_score or 0.0)

        if ws.x_data_provider == "fixture":
            # Offline demo data has no real replies to measure reach from: keep the similarity rule
            def _cand_qualifying_score(c: DiscoveredCandidate) -> float:
                if int(c.reply_sample_count or 0) > 0:
                    return float(c.yield_score or 0.0)
                return max(float(c.yield_score or 0.0), float(c.core_affinity_score or 0.0))

            qualifying = [
                c
                for c in relevant_cands
                if _cand_qualifying_score(c) >= effective_add_threshold
                and (
                    len(core_ties_by_cand.get(c.id, set())) >= 2
                    or has_mutual_conv_by_cand.get(c.id, False)
                    or (
                        int(c.reply_sample_count or 0) >= 1
                        and float(c.yield_score or 0.0) >= effective_add_threshold
                    )
                )
            ]
        else:
            qualifying = [
                c
                for c in relevant_cands
                if (is_measured(c) and float(c.reply_reach_score) >= cand_add_threshold)
                or (
                    int(c.reply_sample_count or 0) >= 1
                    and float(c.yield_score or 0.0) >= effective_add_threshold
                )
            ]
        qualifying.sort(
            key=lambda c: (
                -_cand_qualifying_score(c),
                -float(c.core_affinity_score or 0.0),
                c.id,
            )
        )

        # No tracked-account cap: the add/prune thresholds settle the list on their own, and
        # the monthly unit budget throttle (80% / 95% projected) is the cost guard.
        for cand in qualifying:
            proj = cand.community_projection_json or {}
            chosen_cids = [
                comm_by_slug[slug].id
                for slug, weight in sorted(proj.items(), key=lambda kv: -float(kv[1]))
                if slug in comm_by_slug and float(weight) >= 0.25
            ]
            acct = await promote_candidate_to_community(
                session,
                workspace_id,
                cand.id,
                chosen_cids or None,
                is_tracked=True,
                is_auto_tracked=True,
                auto_tracked_at=now_utc,
                commit=False,
            )
            auto_added_handles.append(acct.handle)
            newly_promoted_accts.append(acct)

    # 3. Garbage-collect stale candidate staging rows & orphaned/converted endorsements
    active_ids_post_promo = {a.id for a in active_tracked_accts} | {
        a.id for a in newly_promoted_accts
    }
    known_acct_handles = {
        a.handle.strip().lstrip("@").lower() for a in refresh_bundle.all_ws_accounts
    } | {a.handle.strip().lstrip("@").lower() for a in newly_promoted_accts}

    candidates_cleaned, endorsements_cleaned = await cleanup_stale_candidates(
        session,
        workspace_id,
        candidates=refresh_bundle.all_candidates,
        endorsements=refresh_bundle.all_endorsements,
        active_account_ids=active_ids_post_promo,
        known_account_handles=known_acct_handles,
        outcomes_by_handle=refresh_bundle.outcomes_by_handle,
        now_utc=now_utc,
    )

    if auto_added_handles or auto_pruned_handles:
        await session.flush()
        await recompute_workspace_graph(
            session, workspace_id, commit=True, reference_time=now_utc
        )
    else:
        await session.commit()

    active_tracked_count = len(active_ids_post_promo)
    post_proj = await estimate_workspace_monthly_units(session, ws, now=now_utc)

    return {
        "workspace_id": workspace_id,
        "auto_added_count": len(auto_added_handles),
        "auto_pruned_count": len(auto_pruned_handles),
        "active_tracked_count": active_tracked_count,
        "profiles_refreshed": profiles_refreshed,
        "own_replies_synced": own_replies_synced,
        "candidates_cleaned": candidates_cleaned,
        "endorsements_cleaned": endorsements_cleaned,
        "projected_monthly_units": float(post_proj["projected_monthly_units"]),
        "monthly_unit_budget": int(post_proj["monthly_unit_budget"]),
        "budget_throttle_mode": str(post_proj["discovery_throttle_mode"]),
        "add_threshold_used": effective_add_threshold,
        "prune_threshold_used": CORE_AFFINITY_PRUNE_THRESHOLD,
        "reach_add_threshold": reach_add_threshold,
        "reach_prune_threshold": reach_prune_threshold,
        "auto_added_handles": auto_added_handles,
        "auto_pruned_handles": auto_pruned_handles,
    }
