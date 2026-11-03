"""End-to-end Epic 1 Graph Analytics Orchestrator & SQLAlchemy Persistence Service."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tweettracker.graph.bridges import BridgesResult, compute_bridges_and_layout
from tweettracker.graph.builder import (
    AccountNodeInput,
    BehavioralGraphBundle,
    PostInteractionInput,
    RelationshipEdgeInput,
    build_behavioral_graph,
)
from tweettracker.graph.heavy_hitters import (
    HeavyHittersResult,
    PostTrajectoryInput,
    compute_heavy_hitters,
)
from tweettracker.graph.nmf_simclusters import (
    CommunityAnchorInput,
    SimClustersResult,
    build_core_graph_context,
    compute_core_affinity_scores,
    compute_mini_simclusters,
)
from tweettracker.graph.relationships import (
    RelationshipsResult,
    compute_relationships_and_roi,
)
from tweettracker.graph.reply_yield import (
    SEED_BOOTSTRAP_GRACE_DAYS,
    AccountYieldMetrics,
    ReplyOutcomeSample,
    attribute_workspace_follower_gains,
    compute_account_yield_metrics,
    compute_reach_prior_score,
    compute_workspace_reference_reply_views,
)
from tweettracker.models import (
    AccountCommunityMembership,
    Community,
    OwnReplyOutcome,
    Post,
    PostInteraction,
    PostSnapshot,
    RelationshipEdge,
    TrackedAccount,
    Workspace,
)

ACTIVE_COMMUNITY_AFFINITY_FLOOR = 0.25


@dataclass(slots=True)
class WorkspaceGraphAnalytics:
    bundle: BehavioralGraphBundle
    simclusters: SimClustersResult
    heavy_hitters: HeavyHittersResult
    bridges: BridgesResult
    relationships: RelationshipsResult
    compute_duration_ms: float
    yield_by_account: dict[int, AccountYieldMetrics] = field(default_factory=dict)


@dataclass(slots=True)
class RecomputeSummary:
    workspace_id: int
    communities_count: int
    accounts_count: int
    edges_count: int
    bridges_count: int
    mutual_active_count: int
    compute_duration_ms: float


@dataclass(slots=True)
class RecomputeWithContextResult:
    summary: RecomputeSummary
    analytics: WorkspaceGraphAnalytics
    workspace: Workspace
    communities: list[Community]
    accounts: list[TrackedAccount]
    memberships: list[AccountCommunityMembership]
    relationship_edges: list[RelationshipEdge]
    posts: list[Post]
    interactions: list[PostInteraction]
    outcomes_by_handle: dict[str, list[ReplyOutcomeSample]]


def _ensure_utc(dt: datetime | None, fallback: datetime | None = None) -> datetime:
    if dt is None:
        return fallback if fallback is not None else datetime.now(timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def compute_workspace_analytics_in_memory(
    *,
    my_x_handle: str | None,
    communities: Sequence[CommunityAnchorInput],
    accounts: Sequence[AccountNodeInput],
    manual_tags: dict[int, set[int]],
    relationship_edges: Sequence[RelationshipEdgeInput],
    posts: Sequence[PostTrajectoryInput] = (),
    post_interactions: Sequence[PostInteractionInput] = (),
    reply_outcomes_by_handle: dict[str, Sequence[ReplyOutcomeSample]] | None = None,
    reference_time: datetime | None = None,
) -> WorkspaceGraphAnalytics:
    """Executes the full Epic 1 5-stage graph pipeline + Yield-Learned Core in memory (<1.5s for 5,000 nodes)."""
    t0 = time.perf_counter()
    now = _ensure_utc(reference_time)
    has_own_h = bool(my_x_handle and my_x_handle.strip())

    acct_foll_by_id = {a.id: int(a.followers_count or 0) for a in accounts}
    climbing_cutoff = now - timedelta(days=SEED_BOOTSTRAP_GRACE_DAYS)
    climbing_authors: set[int] = {
        a.id for a in accounts if bool(a.has_recent_climbing_post)
    }
    for p in posts:
        if p.is_reply or p.is_retweet:
            continue
        if _ensure_utc(p.created_at_x, now) < climbing_cutoff:
            continue
        foll = acct_foll_by_id.get(p.author_account_id, 0)
        view_floor = max(1_500, int(foll * 0.25))
        if int(p.latest_views or 0) >= view_floor or float(p.value_model_score or 0.0) >= 5.0:
            climbing_authors.add(p.author_account_id)

    all_outcome_samples: list[ReplyOutcomeSample] = []
    if reply_outcomes_by_handle is not None:
        for samples in reply_outcomes_by_handle.values():
            all_outcome_samples.extend(samples)
    workspace_ref_views = compute_workspace_reference_reply_views(all_outcome_samples)

    bundle = build_behavioral_graph(
        accounts,
        relationship_edges,
        post_interactions,
        reference_time=now,
    )

    simclusters = compute_mini_simclusters(
        bundle,
        communities,
        manual_tags,
    )

    heavy = compute_heavy_hitters(
        accounts,
        bundle,
        posts=posts,
        post_interactions=post_interactions,
        interested_in=simclusters.interested_in,
        known_for=simclusters.known_for,
        reference_time=now,
    )

    cred_map = {u: m.user_cred_v2 for u, m in heavy.by_account.items()}

    def _eval_yield_map(aff_scores: dict[int, float]) -> dict[int, AccountYieldMetrics]:
        out_map: dict[int, AccountYieldMetrics] = {}
        for acct in accounts:
            is_mut = has_own_h and bool(acct.follows_me and acct.followed_by_me)
            has_climb = acct.id in climbing_authors
            prior = compute_reach_prior_score(
                core_affinity_score=aff_scores.get(acct.id, 0.0),
                followers_count=acct.followers_count,
                has_recent_climbing_post=has_climb,
                is_mutual=is_mut,
                follows_me=acct.follows_me,
            )
            anchor_dt = acct.auto_tracked_at if acct.is_auto_tracked else acct.created_at
            if reply_outcomes_by_handle is not None:
                h_key = acct.handle.strip().lstrip("@").lower()
                samples = reply_outcomes_by_handle.get(h_key, ())
                out_map[acct.id] = compute_account_yield_metrics(
                    prior_score=prior,
                    outcomes=samples,
                    workspace_ref_views=workspace_ref_views,
                    is_mutual=is_mut,
                    is_auto_tracked=acct.is_auto_tracked,
                    created_or_added_at=anchor_dt,
                    has_recent_climbing_post=has_climb,
                    reference_time=now,
                )
            elif int(acct.reply_sample_count or 0) > 0 and acct.yield_score is not None:
                out_map[acct.id] = AccountYieldMetrics(
                    prior_score=prior,
                    empirical_yield=float(acct.yield_score),
                    yield_score=float(acct.yield_score),
                    reply_sample_count=int(acct.reply_sample_count),
                    eff_sample_weight=float(acct.reply_sample_count),
                    avg_reply_views=float(acct.avg_reply_views or 0.0),
                    attributed_follows_30d=float(acct.attributed_follows_30d or 0.0),
                )
            else:
                out_map[acct.id] = compute_account_yield_metrics(
                    prior_score=prior,
                    outcomes=(),
                    workspace_ref_views=workspace_ref_views,
                    is_mutual=is_mut,
                    is_auto_tracked=acct.is_auto_tracked,
                    created_or_added_at=anchor_dt,
                    has_recent_climbing_post=has_climb,
                    reference_time=now,
                )
        return out_map

    # Pass 1: score SimClusters core affinity & empirical reply yield
    aff_pass1 = compute_core_affinity_scores(
        accounts=accounts,
        bundle=bundle,
        simclusters=simclusters,
        pr_by_node=heavy.pr_by_account,
        out_deg_by_node=heavy.out_deg_by_account,
        cred_by_node=cred_map,
        my_x_handle=my_x_handle,
        reference_time=now,
    )
    core_ids_pass1 = set(simclusters.core_anchor_ids)
    yield_pass1 = _eval_yield_map(aff_pass1)

    has_empirical_samples = any(m.reply_sample_count > 0 for m in yield_pass1.values())
    any_seed_past_grace = any(
        (not a.is_auto_tracked)
        and a.created_at is not None
        and _ensure_utc(a.created_at, now) < climbing_cutoff
        for a in accounts
    )
    if has_empirical_samples or any_seed_past_grace:
        enriched_accounts = [
            replace(
                a,
                yield_score=yield_pass1[a.id].yield_score,
                reply_sample_count=yield_pass1[a.id].reply_sample_count,
                avg_reply_views=yield_pass1[a.id].avg_reply_views,
                attributed_follows_30d=yield_pass1[a.id].attributed_follows_30d,
                has_recent_climbing_post=(a.id in climbing_authors),
            )
            for a in accounts
        ]
        ctx_pass2 = build_core_graph_context(
            enriched_accounts,
            my_x_handle=my_x_handle,
            auto_cluster_ids=simclusters.auto_cluster_ids,
            primary_community=simclusters.primary_community,
            pr_by_node=heavy.pr_by_account,
            out_deg_by_node=heavy.out_deg_by_account,
            cred_by_node=cred_map,
            reference_time=now,
        )
        if ctx_pass2.core_ids != core_ids_pass1 or has_empirical_samples:
            final_aff = compute_core_affinity_scores(
                accounts=enriched_accounts,
                bundle=bundle,
                simclusters=simclusters,
                pr_by_node=heavy.pr_by_account,
                out_deg_by_node=heavy.out_deg_by_account,
                cred_by_node=cred_map,
                my_x_handle=my_x_handle,
                reference_time=now,
            )
            final_yield = _eval_yield_map(final_aff)
        else:
            final_aff = aff_pass1
            final_yield = yield_pass1
    else:
        final_aff = aff_pass1
        final_yield = yield_pass1

    simclusters.core_affinity = final_aff
    simclusters.yield_scores = {u: m.yield_score for u, m in final_yield.items()}

    hh_map = {u: m.heavy_hitter_score for u, m in heavy.by_account.items()}

    bridges = compute_bridges_and_layout(
        bundle,
        communities,
        interested_in=simclusters.interested_in,
        known_for=simclusters.known_for,
        heavy_hitter_scores=hh_map,
        sub_pocket_labels=simclusters.sub_pocket_labels,
    )

    bs_map = {u: b.bridge_score for u, b in bridges.by_account.items()}

    relationships = compute_relationships_and_roi(
        my_x_handle,
        accounts,
        heavy_hitter_scores=hh_map,
        bridge_scores=bs_map,
        post_interactions=post_interactions,
        reference_time=now,
    )

    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
    return WorkspaceGraphAnalytics(
        bundle=bundle,
        simclusters=simclusters,
        heavy_hitters=heavy,
        bridges=bridges,
        relationships=relationships,
        compute_duration_ms=elapsed_ms,
        yield_by_account=final_yield,
    )


def _extract_p50_curve(baseline_curves_json: dict | None) -> dict[int, float]:
    if not isinstance(baseline_curves_json, dict):
        return {}
    p50_raw = baseline_curves_json.get("p50")
    if not isinstance(p50_raw, dict):
        return {}
    out: dict[int, float] = {}
    for k, v in p50_raw.items():
        try:
            out[int(k)] = float(v)
        except (TypeError, ValueError):
            continue
    return out


async def load_workspace_graph_inputs(
    session: AsyncSession,
    workspace_id: int,
) -> tuple[
    Workspace,
    list[Community],
    list[TrackedAccount],
    list[AccountCommunityMembership],
    list[RelationshipEdge],
    list[Post],
    dict[int, list[tuple[int, int]]],
    list[PostInteraction],
]:
    ws = (
        await session.execute(select(Workspace).where(Workspace.id == workspace_id))
    ).scalar_one_or_none()
    if ws is None:
        raise ValueError(f"Workspace {workspace_id} not found.")

    comms = list(
        (
            await session.execute(
                select(Community)
                .where(Community.workspace_id == workspace_id)
                .order_by(Community.sort_order.asc(), Community.id.asc())
            )
        ).scalars().all()
    )

    accts = list(
        (
            await session.execute(
                select(TrackedAccount)
                .where(
                    TrackedAccount.workspace_id == workspace_id,
                    TrackedAccount.is_tracked.is_(True),
                    TrackedAccount.is_muted.is_(False),
                )
                .order_by(TrackedAccount.id.asc())
            )
        ).scalars().all()
    )

    memberships = list(
        (
            await session.execute(
                select(AccountCommunityMembership).where(
                    AccountCommunityMembership.workspace_id == workspace_id
                )
            )
        ).scalars().all()
    )

    rel_edges = list(
        (
            await session.execute(
                select(RelationshipEdge).where(RelationshipEdge.workspace_id == workspace_id)
            )
        ).scalars().all()
    )

    posts = list(
        (
            await session.execute(
                select(Post).where(Post.workspace_id == workspace_id)
            )
        ).scalars().all()
    )

    post_ids = [p.id for p in posts]
    snaps_by_post: dict[int, list[tuple[int, int]]] = {pid: [] for pid in post_ids}
    if post_ids:
        snap_rows = (
            await session.execute(
                select(PostSnapshot.post_id, PostSnapshot.age_minutes, PostSnapshot.views)
                .where(PostSnapshot.post_id.in_(post_ids))
                .order_by(PostSnapshot.age_minutes.asc())
            )
        ).all()
        for pid, age_m, views in snap_rows:
            snaps_by_post.setdefault(int(pid), []).append((int(age_m), int(views)))

    interactions = list(
        (
            await session.execute(
                select(PostInteraction).where(PostInteraction.workspace_id == workspace_id)
            )
        ).scalars().all()
    )

    return ws, comms, accts, memberships, rel_edges, posts, snaps_by_post, interactions


def build_analytics_from_orm(
    ws: Workspace,
    comms: Sequence[Community],
    accts: Sequence[TrackedAccount],
    memberships: Sequence[AccountCommunityMembership],
    rel_edges: Sequence[RelationshipEdge],
    posts: Sequence[Post],
    snaps_by_post: dict[int, list[tuple[int, int]]],
    interactions: Sequence[PostInteraction],
    *,
    reply_outcomes_by_handle: dict[str, Sequence[ReplyOutcomeSample]] | None = None,
    reference_time: datetime | None = None,
) -> WorkspaceGraphAnalytics:
    now_utc = _ensure_utc(reference_time)
    climbing_cutoff = now_utc - timedelta(days=SEED_BOOTSTRAP_GRACE_DAYS)
    foll_by_id = {a.id: int(a.followers_count or 0) for a in accts}
    recent_climbing_authors: set[int] = set()
    for p in posts:
        if p.is_reply or p.is_retweet:
            continue
        if _ensure_utc(p.created_at_x, now_utc) < climbing_cutoff:
            continue
        foll = foll_by_id.get(p.author_account_id, 0)
        if float(p.breakout_score or 0.0) >= 55.0 or int(p.latest_views or 0) >= max(
            1_500, int(foll * 0.25)
        ):
            recent_climbing_authors.add(p.author_account_id)

    comm_inputs = [
        CommunityAnchorInput(id=c.id, name=c.name, slug=c.slug, color=c.color)
        for c in comms
    ]
    acct_inputs = [
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
            last_interaction_at=a.last_interaction_at,
            mutual_interaction_count_30d=a.mutual_interaction_count_30d,
            baseline_p50=_extract_p50_curve(a.baseline_curves_json),
            yield_score=(
                float(a.yield_score)
                if (int(a.reply_sample_count or 0) > 0 or float(a.yield_score or 0.0) > 0.0)
                else None
            ),
            reply_sample_count=int(a.reply_sample_count or 0),
            avg_reply_views=float(a.avg_reply_views or 0.0),
            attributed_follows_30d=float(a.attributed_follows_30d or 0.0),
            has_recent_climbing_post=(a.id in recent_climbing_authors),
        )
        for a in accts
    ]

    manual_tags: dict[int, set[int]] = {a.id: set() for a in accts}
    for m in memberships:
        if m.is_manual_tag and m.account_id in manual_tags:
            manual_tags[m.account_id].add(m.community_id)

    edge_inputs = [
        RelationshipEdgeInput(
            source_account_id=e.source_account_id,
            target_account_id=e.target_account_id,
            edge_type=e.edge_type,
            interaction_count=e.interaction_count,
            last_event_at=e.last_event_at,
        )
        for e in rel_edges
    ]

    post_author_by_id = {p.id: p.author_account_id for p in posts}
    post_inputs = [
        PostTrajectoryInput(
            id=p.id,
            x_post_id=p.x_post_id,
            author_account_id=p.author_account_id,
            created_at_x=p.created_at_x,
            is_reply=p.is_reply,
            is_retweet=p.is_retweet,
            is_quote=p.is_quote,
            latest_views=p.latest_views,
            value_model_score=p.value_model_score,
            snapshots=snaps_by_post.get(p.id, []),
        )
        for p in posts
    ]

    pi_inputs = [
        PostInteractionInput(
            post_id=pi.post_id,
            author_account_id=post_author_by_id.get(pi.post_id, -1),
            actor_account_id=pi.actor_account_id,
            actor_x_user_id=pi.actor_x_user_id,
            actor_handle=pi.actor_handle,
            interaction_type=pi.interaction_type,
            occurred_at=pi.occurred_at,
        )
        for pi in interactions
        if pi.post_id in post_author_by_id
    ]

    return compute_workspace_analytics_in_memory(
        my_x_handle=ws.my_x_handle,
        communities=comm_inputs,
        accounts=acct_inputs,
        manual_tags=manual_tags,
        relationship_edges=edge_inputs,
        posts=post_inputs,
        post_interactions=pi_inputs,
        reply_outcomes_by_handle=reply_outcomes_by_handle,
        reference_time=reference_time,
    )


async def recompute_workspace_graph_with_context(
    session: AsyncSession,
    workspace_id: int,
    *,
    commit: bool = True,
    reference_time: datetime | None = None,
) -> RecomputeWithContextResult:
    """Runs the full Epic 1 pipeline + Yield-Learned Core, persists scores, and returns loaded ORM context + analytics."""
    now_utc = _ensure_utc(reference_time)
    await attribute_workspace_follower_gains(session, workspace_id, now=now_utc)

    (
        ws,
        comms,
        accts,
        memberships,
        rel_edges,
        posts,
        snaps_by_post,
        interactions,
    ) = await load_workspace_graph_inputs(session, workspace_id)

    acct_by_handle = {a.handle.strip().lstrip("@").lower(): a for a in accts}
    outcome_rows = list(
        (
            await session.execute(
                select(OwnReplyOutcome).where(OwnReplyOutcome.workspace_id == workspace_id)
            )
        )
        .scalars()
        .all()
    )
    outcomes_by_handle: dict[str, list[ReplyOutcomeSample]] = defaultdict(list)
    for o in outcome_rows:
        h_clean = (o.target_handle or "").strip().lstrip("@").lower()
        if not h_clean:
            continue
        if o.target_account_id is None and h_clean in acct_by_handle:
            o.target_account_id = acct_by_handle[h_clean].id
        outcomes_by_handle[h_clean].append(
            ReplyOutcomeSample(
                x_reply_post_id=o.x_reply_post_id,
                target_handle=h_clean,
                created_at_x=_ensure_utc(o.created_at_x, now_utc),
                views=int(o.latest_views or 0),
                attributed_follows_gained=float(o.attributed_follows_gained or 0.0),
                action_kind=o.action_kind,
                was_mutual_at_reply=bool(o.was_mutual_at_reply),
            )
        )

    analytics = build_analytics_from_orm(
        ws,
        comms,
        accts,
        memberships,
        rel_edges,
        posts,
        snaps_by_post,
        interactions,
        reply_outcomes_by_handle=outcomes_by_handle,
        reference_time=now_utc,
    )

    # Persist TrackedAccount analytics & empirical reply yield
    for acct in accts:
        u = acct.id
        hh = analytics.heavy_hitters.by_account.get(u)
        br = analytics.bridges.by_account.get(u)
        rel = analytics.relationships.by_account.get(u)
        ym = analytics.yield_by_account.get(u)

        if hh is not None:
            acct.heavy_hitter_score = hh.heavy_hitter_score
            acct.catalysis_lift = hh.catalysis_lift
            acct.s_core_depth = hh.s_core_depth
            acct.salsa_authority = hh.salsa_authority
            acct.salsa_scout = hh.salsa_scout
            acct.user_cred_v2 = hh.user_cred_v2

        if br is not None:
            acct.bridge_score = br.bridge_score
            acct.participation_coeff = br.participation_coeff
            acct.burt_brokerage = br.burt_brokerage
            acct.bridge_role = br.bridge_role

        acct.core_affinity_score = float(analytics.simclusters.core_affinity.get(u, 0.0))
        if ym is not None:
            acct.yield_score = ym.yield_score
            acct.reply_sample_count = ym.reply_sample_count
            acct.avg_reply_views = ym.avg_reply_views
            acct.attributed_follows_30d = ym.attributed_follows_30d
        acct.sub_pocket_label = analytics.simclusters.sub_pocket_labels.get(u)

        if rel is not None:
            acct.relationship_state = rel.relationship_state
            if analytics.relationships.has_own_handle:
                acct.mutual_interaction_count_30d = rel.mutual_interaction_count_30d
                acct.last_interaction_at = rel.last_interaction_at

    # Update RelationshipEdge decayed weights from 14d behavioral graph
    now = reference_time or datetime.now(timezone.utc)
    from tweettracker.graph.builder import (
        FOLLOW_WEIGHT,
        MUTUAL_FOLLOW_BONUS,
        MUTUAL_REPLY_WEIGHT,
        QUOTE_WEIGHT,
        REPLY_WEIGHT,
        REPOST_WEIGHT,
        decay_multiplier,
    )

    for e in rel_edges:
        pair = (e.source_account_id, e.target_account_id)
        is_mut = pair in analytics.bundle.mutual_follow_pairs
        etype = e.edge_type.strip().lower()
        if etype == "follow":
            e.decayed_weight = round(FOLLOW_WEIGHT + (MUTUAL_FOLLOW_BONUS if is_mut else 0.0), 4)
        else:
            if etype == "reply":
                bw = MUTUAL_REPLY_WEIGHT if is_mut else REPLY_WEIGHT
            elif etype == "quote":
                bw = QUOTE_WEIGHT
            elif etype == "repost":
                bw = REPOST_WEIGHT
            else:
                bw = 1.0
            dec = decay_multiplier(e.last_event_at, now)
            e.decayed_weight = round(max(1, e.interaction_count) * bw * dec, 4)

    # Persist ONLY active AccountCommunityMembership rows (manual tags or NMF affinity >= 0.25,
    # with fallback to single primary community if K >= 5 diffuses all weights < 0.25).
    existing_mem_map: dict[tuple[int, int], AccountCommunityMembership] = {
        (m.account_id, m.community_id): m for m in memberships
    }
    valid_comm_ids = {c.id for c in comms}
    tracked_ids = {a.id for a in accts}

    # Remove stale non-valid community memberships if any
    for (aid, cid), m_row in list(existing_mem_map.items()):
        if cid not in valid_comm_ids or aid not in tracked_ids:
            await session.delete(m_row)
            existing_mem_map.pop((aid, cid), None)

    for acct in accts:
        u = acct.id
        kf_map = analytics.simclusters.known_for.get(u, {})
        in_map = analytics.simclusters.interested_in.get(u, {})

        qualifying_cids: set[int] = set()
        best_cid: int | None = None
        best_aff = -1.0

        for comm in comms:
            cid = comm.id
            kf_val = float(kf_map.get(cid, 0.0))
            in_val = float(in_map.get(cid, 0.0))
            max_aff = max(kf_val, in_val)
            if max_aff > best_aff:
                best_aff = max_aff
                best_cid = cid
            existing_row = existing_mem_map.get((u, cid))
            is_man = bool(existing_row.is_manual_tag) if existing_row is not None else False
            if is_man or max_aff >= ACTIVE_COMMUNITY_AFFINITY_FLOOR:
                qualifying_cids.add(cid)

        if not qualifying_cids and comms:
            prim_cid = analytics.simclusters.primary_community.get(u)
            if prim_cid in valid_comm_ids:
                qualifying_cids.add(prim_cid)
            elif best_cid is not None:
                qualifying_cids.add(best_cid)

        for comm in comms:
            cid = comm.id
            kf_val = float(kf_map.get(cid, 0.0))
            in_val = float(in_map.get(cid, 0.0))
            existing_row = existing_mem_map.get((u, cid))
            if cid in qualifying_cids:
                if existing_row is not None:
                    existing_row.nmf_known_for = kf_val
                    existing_row.nmf_interested_in = in_val
                else:
                    new_mem = AccountCommunityMembership(
                        workspace_id=workspace_id,
                        account_id=u,
                        community_id=cid,
                        is_manual_tag=False,
                        nmf_known_for=kf_val,
                        nmf_interested_in=in_val,
                    )
                    session.add(new_mem)
                    existing_mem_map[(u, cid)] = new_mem
            elif existing_row is not None:
                await session.delete(existing_row)
                existing_mem_map.pop((u, cid), None)

    await session.flush()
    if commit:
        await session.commit()

    bridges_cnt = sum(
        1 for b in analytics.bridges.by_account.values() if b.bridge_role != "provincial"
    )
    mutual_active_cnt = analytics.relationships.counts.get("mutual_active", 0)

    summary = RecomputeSummary(
        workspace_id=workspace_id,
        communities_count=len(comms),
        accounts_count=len(accts),
        edges_count=analytics.bundle.directed.number_of_edges(),
        bridges_count=bridges_cnt,
        mutual_active_count=mutual_active_cnt,
        compute_duration_ms=analytics.compute_duration_ms,
    )
    return RecomputeWithContextResult(
        summary=summary,
        analytics=analytics,
        workspace=ws,
        communities=comms,
        accounts=accts,
        memberships=list(existing_mem_map.values()),
        relationship_edges=rel_edges,
        posts=posts,
        interactions=interactions,
        outcomes_by_handle=dict(outcomes_by_handle),
    )


async def recompute_workspace_graph(
    session: AsyncSession,
    workspace_id: int,
    *,
    commit: bool = True,
    reference_time: datetime | None = None,
) -> RecomputeSummary:
    """Runs the full Epic 1 pipeline + Yield-Learned Core and persists scores to TrackedAccount & AccountCommunityMembership."""
    res = await recompute_workspace_graph_with_context(
        session,
        workspace_id,
        commit=commit,
        reference_time=reference_time,
    )
    return res.summary
