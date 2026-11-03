"""Paced Delta Radar Poller & Trajectory Snapshot Scheduler."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tweettracker.algo import compute_text_embedding
from tweettracker.models import (
    AccountCommunityMembership,
    AlgoParamVersion,
    BreakoutPrediction,
    Community,
    Post,
    PostInteraction,
    PostSnapshot,
    TrackedAccount,
    Workspace,
)
from tweettracker.providers import PostRecord, get_x_data_provider
from tweettracker.radar.scorer import (
    RadarCandidateBundle,
    ScoredPostResult,
    load_active_algo_bundle,
    score_radar_batch,
)
from tweettracker.radar.sse import RadarSSEHub, radar_sse_hub
from tweettracker.radar.velocity import BASELINE_AGES, interpolate_baseline_quantiles
from tweettracker.radar.webhooks import dispatch_breakout_alert

SNAPSHOT_AGES: tuple[int, ...] = BASELINE_AGES

# Export alias matching spec naming (`get_x_provider` / `get_x_data_provider`)
get_x_provider = get_x_data_provider


def _ensure_utc(dt: datetime | None) -> datetime:
    if dt is None:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _snowflake_key(raw_id: str) -> tuple[int, int, str]:
    cleaned = raw_id.strip()
    if cleaned.isdigit():
        return (1, int(cleaned), "")
    return (0, 0, cleaned)


def _snowflake_gt(post_id: str, since_id: str) -> bool:
    return _snowflake_key(post_id) > _snowflake_key(since_id)


def _is_active_membership(mem: AccountCommunityMembership) -> bool:
    return bool(
        mem.is_manual_tag
        or max(float(mem.nmf_interested_in or 0.0), float(mem.nmf_known_for or 0.0)) >= 0.25
    )


def select_snapshot_bucket(age_minutes: int) -> int:
    """Maps post age (minutes) to the highest reached canonical snapshot bucket
    (`15, 30, 45, 60, 120, 360, 1440, 2880`), falling back to `15` when `< 15m`.
    """
    clamped = max(1, int(age_minutes))
    reached = [b for b in SNAPSHOT_AGES if clamped >= b]
    return max(reached) if reached else SNAPSHOT_AGES[0]


@dataclass(slots=True)
class RadarPollSummary:
    """Result summary from a paced workspace radar poll & rescore pass."""

    workspace_id: int
    provider: str
    polled_posts_count: int
    ingested_new_posts_count: int
    active_posts_count: int
    matured_now_count: int
    snapshots_upserted_count: int
    predictions_recorded_count: int
    watch_count: int
    surge_count: int
    new_alerts_count: int
    sse_events_emitted: int
    webhook_deliveries_attempted: int
    scored_results: list[ScoredPostResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "provider": self.provider,
            "polled_posts_count": self.polled_posts_count,
            "ingested_new_posts_count": self.ingested_new_posts_count,
            "active_posts_count": self.active_posts_count,
            "matured_now_count": self.matured_now_count,
            "snapshots_upserted_count": self.snapshots_upserted_count,
            "predictions_recorded_count": self.predictions_recorded_count,
            "watch_count": self.watch_count,
            "surge_count": self.surge_count,
            "new_alerts_count": self.new_alerts_count,
            "sse_events_emitted": self.sse_events_emitted,
            "webhook_deliveries_attempted": self.webhook_deliveries_attempted,
        }


async def poll_workspace_radar(
    session: AsyncSession,
    workspace: Workspace,
    *,
    now: datetime | None = None,
    sse_hub: RadarSSEHub | None = None,
    retry_delays: Sequence[float] | None = None,
) -> RadarPollSummary:
    """Polls tracked accounts' latest posts via `get_x_provider(workspace.x_data_provider)`,
    upserts `Post` + immutable `PostSnapshot` (`15..2880m`) + `PostInteraction`s, runs Stage 1 + Stage 2
    `scorer.py`, persists `BreakoutPrediction`, emits SSE events, and dispatches HMAC webhooks
    for newly crossed `watch` / `surge` tiers.
    """
    now_utc = _ensure_utc(now)
    hub = sse_hub or radar_sse_hub
    provider = get_x_provider(workspace.x_data_provider)
    algo_bundle = await load_active_algo_bundle(session, workspace.id, ensure_db_row=True)

    active_ver_row = (
        await session.execute(
            select(AlgoParamVersion).where(AlgoParamVersion.id == algo_bundle.param_version_id)
        )
    ).scalar_one_or_none()
    thresholds_cfg: Mapping[str, Any] = (
        active_ver_row.thresholds_json
        if (active_ver_row is not None and isinstance(active_ver_row.thresholds_json, Mapping))
        else {}
    )
    bo_mult = float(thresholds_cfg.get("breakout_multiple", 3.0))
    global_floor_views = int(thresholds_cfg.get("community_floor_views", 5_000))
    raw_comm_floors = thresholds_cfg.get("community_floors")
    comm_floors_map: dict[str, int] = (
        {str(k): int(v) for k, v in raw_comm_floors.items()}
        if isinstance(raw_comm_floors, Mapping)
        else {}
    )

    # 1. Load tracked accounts & active community memberships
    accounts = list(
        (
            await session.execute(
                select(TrackedAccount).where(
                    TrackedAccount.workspace_id == workspace.id,
                    TrackedAccount.is_tracked.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    accounts_by_id: dict[int, TrackedAccount] = {a.id: a for a in accounts}
    accounts_by_handle: dict[str, TrackedAccount] = {a.handle.lower(): a for a in accounts}
    accounts_by_x_uid: dict[str, TrackedAccount] = {a.x_user_id: a for a in accounts}

    communities = list(
        (
            await session.execute(
                select(Community)
                .where(Community.workspace_id == workspace.id)
                .order_by(Community.sort_order.asc(), Community.id.asc())
            )
        )
        .scalars()
        .all()
    )
    comms_by_id: dict[int, Community] = {c.id: c for c in communities}

    memberships = list(
        (
            await session.execute(
                select(AccountCommunityMembership).where(
                    AccountCommunityMembership.workspace_id == workspace.id
                )
            )
        )
        .scalars()
        .all()
    )
    acct_comms: dict[int, list[Community]] = {}
    comm_member_handles: dict[int, set[str]] = {}
    for mem in memberships:
        if not _is_active_membership(mem):
            continue
        comm = comms_by_id.get(mem.community_id)
        acct = accounts_by_id.get(mem.account_id)
        if comm is not None:
            acct_comms.setdefault(mem.account_id, []).append(comm)
            if acct is not None:
                comm_member_handles.setdefault(comm.id, set()).add(acct.handle.lower())

    # 2. Fetch delta posts via provider (`list_id` when use_x_lists is active, else `handles`)
    fetched_by_x_id: dict[str, PostRecord] = {}
    if accounts:
        covered_handles: set[str] = set()
        if workspace.use_x_lists:
            seen_list_ids: set[str] = set()
            ws_list_ids = [
                str(lid).strip()
                for lid in (workspace.x_list_ids_json or [])
                if str(lid).strip()
            ]
            for lid in ws_list_ids:
                if lid in seen_list_ids:
                    continue
                seen_list_ids.add(lid)
                recs = await provider.search_recent_posts(
                    session,
                    workspace,
                    list_id=lid,
                    exclude_replies=True,
                )
                for r in recs:
                    fetched_by_x_id[r.x_post_id] = r
            if ws_list_ids:
                covered_handles.update(a.handle.lower() for a in accounts)

            for comm in communities:
                if comm.x_list_id and comm.x_list_id not in seen_list_ids:
                    seen_list_ids.add(comm.x_list_id)
                    recs = await provider.search_recent_posts(
                        session,
                        workspace,
                        list_id=comm.x_list_id,
                        exclude_replies=True,
                    )
                    for r in recs:
                        fetched_by_x_id[r.x_post_id] = r
                    covered_handles.update(comm_member_handles.get(comm.id, set()))

        remaining_accounts = [
            a for a in accounts if a.handle.lower() not in covered_handles
        ]
        if remaining_accounts:
            # Only pass global since_id when every remaining account has already been synced
            since_ids = [a.last_synced_since_id for a in remaining_accounts]
            common_since_id = (
                min((s for s in since_ids if s), key=_snowflake_key)
                if (since_ids and all(s is not None for s in since_ids))
                else None
            )
            recs = await provider.search_recent_posts(
                session,
                workspace,
                handles=[a.handle for a in remaining_accounts],
                since_id=common_since_id,
                exclude_replies=True,
            )
            for r in recs:
                fetched_by_x_id[r.x_post_id] = r

    # Refresh active (<48h) stored posts in 200-ID batches
    active_existing = list(
        (
            await session.execute(
                select(Post).where(
                    Post.workspace_id == workspace.id,
                    Post.matured_48h.is_(False),
                )
            )
        )
        .scalars()
        .all()
    )
    existing_by_x_id: dict[str, Post] = {p.x_post_id: p for p in active_existing}
    missing_refresh_ids = [
        p.x_post_id for p in active_existing if p.x_post_id not in fetched_by_x_id
    ]
    for start in range(0, len(missing_refresh_ids), 200):
        batch_ids = missing_refresh_ids[start : start + 200]
        refreshed = await provider.get_posts_by_ids(session, workspace, batch_ids)
        for r in refreshed:
            fetched_by_x_id[r.x_post_id] = r

    # Also check whether any fetched x_post_id already exists as matured_48h in DB
    if fetched_by_x_id:
        all_known_rows = list(
            (
                await session.execute(
                    select(Post).where(
                        Post.workspace_id == workspace.id,
                        Post.x_post_id.in_(list(fetched_by_x_id.keys())),
                    )
                )
            )
            .scalars()
            .all()
        )
        for p in all_known_rows:
            existing_by_x_id[p.x_post_id] = p

    # 3. Upsert Posts & update author last_synced_since_id
    ingested_new = 0
    for x_pid, rec in fetched_by_x_id.items():
        author = accounts_by_handle.get(rec.author_handle.lower()) or accounts_by_x_uid.get(
            rec.author_x_user_id
        )
        if author is None:
            continue

        if (
            author.last_synced_since_id is None
            or _snowflake_gt(rec.x_post_id, author.last_synced_since_id)
        ):
            author.last_synced_since_id = rec.x_post_id

        post_obj = existing_by_x_id.get(x_pid)
        if post_obj is None:
            emb = compute_text_embedding(rec.text)
            post_obj = Post(
                workspace_id=workspace.id,
                x_post_id=rec.x_post_id,
                author_account_id=author.id,
                text=rec.text,
                created_at_x=_ensure_utc(rec.created_at_x),
                is_reply=rec.is_reply,
                is_retweet=rec.is_retweet,
                is_quote=rec.is_quote,
                in_reply_to_post_id=rec.in_reply_to_post_id,
                conversation_id=rec.conversation_id,
                quoted_post_id=rec.quoted_post_id,
                has_url=rec.has_url,
                has_media=rec.has_media,
                media_type=rec.media_type,
                video_duration_ms=rec.video_duration_ms,
                ancestor_author_ids_json=list(rec.ancestor_author_ids or []),
                embedding_json=emb,
                latest_views=max(0, int(rec.views)),
                latest_likes=max(0, int(rec.likes)),
                latest_replies=max(0, int(rec.replies)),
                latest_quotes=max(0, int(rec.quotes)),
                latest_reposts=max(0, int(rec.reposts)),
                latest_bookmarks=max(0, int(rec.bookmarks)),
                matured_48h=False,
            )
            session.add(post_obj)
            existing_by_x_id[x_pid] = post_obj
            ingested_new += 1
        elif not post_obj.matured_48h:
            post_obj.latest_views = max(post_obj.latest_views, int(rec.views))
            post_obj.latest_likes = max(post_obj.latest_likes, int(rec.likes))
            post_obj.latest_replies = max(post_obj.latest_replies, int(rec.replies))
            post_obj.latest_quotes = max(post_obj.latest_quotes, int(rec.quotes))
            post_obj.latest_reposts = max(post_obj.latest_reposts, int(rec.reposts))
            post_obj.latest_bookmarks = max(post_obj.latest_bookmarks, int(rec.bookmarks))
            if not post_obj.embedding_json:
                post_obj.embedding_json = compute_text_embedding(post_obj.text)

    await session.flush()

    # 4. Separate active (<48h) vs newly matured (>=48h) posts
    all_unmatured = list(
        (
            await session.execute(
                select(Post).where(
                    Post.workspace_id == workspace.id,
                    Post.matured_48h.is_(False),
                )
            )
        )
        .scalars()
        .all()
    )

    unmatured_ids = [p.id for p in all_unmatured]
    existing_snap_rows = (
        list(
            (
                await session.execute(
                    select(PostSnapshot)
                    .where(PostSnapshot.post_id.in_(unmatured_ids))
                    .order_by(PostSnapshot.post_id.asc(), PostSnapshot.age_minutes.asc())
                )
            )
            .scalars()
            .all()
        )
        if unmatured_ids
        else []
    )
    snaps_by_post: dict[int, list[PostSnapshot]] = {}
    for s in existing_snap_rows:
        snaps_by_post.setdefault(s.post_id, []).append(s)

    active_posts: list[Post] = []
    matured_now = 0
    snapshots_upserted = 0
    for p in all_unmatured:
        author = accounts_by_id.get(p.author_account_id)
        if author is None:
            continue
        age_m = max(1, int(round((now_utc - _ensure_utc(p.created_at_x)).total_seconds() / 60.0)))
        if age_m >= 2880:
            # Capture terminal 2880m snapshot before marking matured_48h
            p_snaps = snaps_by_post.setdefault(p.id, [])
            if not any(s.age_minutes == 2880 for s in p_snaps):
                snap_48 = PostSnapshot(
                    post_id=p.id,
                    age_minutes=2880,
                    captured_at=now_utc,
                    views=p.latest_views,
                    likes=p.latest_likes,
                    replies=p.latest_replies,
                    quotes=p.latest_quotes,
                    reposts=p.latest_reposts,
                    bookmarks=p.latest_bookmarks,
                    breakout_score_at_capture=p.breakout_score,
                )
                session.add(snap_48)
                p_snaps.append(snap_48)
                snapshots_upserted += 1

            p.matured_48h = True
            p.actual_48h_views = p.latest_views
            interp_48 = interpolate_baseline_quantiles(
                author.baseline_curves_json,
                2880.0,
                followers_fallback=author.followers_count,
            )
            mult_48 = round(float(p.latest_views) / max(interp_48.p50, 1.0), 2)
            p.actual_48h_multiple = mult_48

            eff_floor = global_floor_views
            for c_obj in acct_comms.get(author.id, ()):
                if c_obj.slug in comm_floors_map:
                    eff_floor = min(eff_floor, comm_floors_map[c_obj.slug])
                elif str(c_obj.id) in comm_floors_map:
                    eff_floor = min(eff_floor, comm_floors_map[str(c_obj.id)])
            p.is_true_breakout_48h = bool(mult_48 >= bo_mult and p.latest_views >= eff_floor)
            matured_now += 1
        else:
            active_posts.append(p)

    if not active_posts:
        await session.commit()
        hub.publish(
            workspace.id,
            "radar.polled",
            {
                "polled_posts_count": len(fetched_by_x_id),
                "active_posts_count": 0,
                "watch_count": 0,
                "surge_count": 0,
                "new_alerts_count": 0,
            },
        )
        return RadarPollSummary(
            workspace_id=workspace.id,
            provider=provider.provider_name,
            polled_posts_count=len(fetched_by_x_id),
            ingested_new_posts_count=ingested_new,
            active_posts_count=0,
            matured_now_count=matured_now,
            snapshots_upserted_count=snapshots_upserted,
            predictions_recorded_count=0,
            watch_count=0,
            surge_count=0,
            new_alerts_count=0,
            sse_events_emitted=1,
            webhook_deliveries_attempted=0,
        )

    active_post_ids = [p.id for p in active_posts]

    inter_rows = list(
        (
            await session.execute(
                select(PostInteraction)
                .where(PostInteraction.post_id.in_(active_post_ids))
                .order_by(PostInteraction.occurred_at.asc())
            )
        )
        .scalars()
        .all()
    )
    inters_by_post: dict[int, list[PostInteraction]] = {}
    for inter in inter_rows:
        inters_by_post.setdefault(inter.post_id, []).append(inter)

    # Insert current milestone snapshot bucket once reached (existing milestone buckets stay immutable)
    newly_created_snap_by_post: dict[int, PostSnapshot] = {}
    for p in active_posts:
        age_m = max(1, int(round((now_utc - _ensure_utc(p.created_at_x)).total_seconds() / 60.0)))
        bucket = select_snapshot_bucket(age_m)
        existing_snaps = snaps_by_post.setdefault(p.id, [])
        matched = next((s for s in existing_snaps if s.age_minutes == bucket), None)
        if matched is None:
            matched = PostSnapshot(
                post_id=p.id,
                age_minutes=bucket,
                captured_at=now_utc,
                views=p.latest_views,
                likes=p.latest_likes,
                replies=p.latest_replies,
                quotes=p.latest_quotes,
                reposts=p.latest_reposts,
                bookmarks=p.latest_bookmarks,
                breakout_score_at_capture=p.breakout_score,
            )
            session.add(matched)
            existing_snaps.append(matched)
            existing_snaps.sort(key=lambda item: item.age_minutes)
            newly_created_snap_by_post[p.id] = matched
            snapshots_upserted += 1

    await session.flush()

    # 5. Stage 1 + Stage 2 Batch Scoring
    followed_ids = [
        a.id
        for a in accounts
        if workspace.my_x_handle and a.followed_by_me
    ]
    bundles = [
        RadarCandidateBundle(
            post=p,
            author=accounts_by_id[p.author_account_id],
            communities=acct_comms.get(p.author_account_id, ()),
            snapshots=snaps_by_post.get(p.id, ()),
            interactions=inters_by_post.get(p.id, ()),
        )
        for p in active_posts
    ]
    scored_list = score_radar_batch(
        workspace,
        bundles,
        algo_bundle=algo_bundle,
        actor_accounts_by_id=accounts_by_id,
        followed_author_ids=followed_ids,
        now=now_utc,
        enforce_age_48h_drop=True,
    )

    # Check latest stored prediction per post so we record clean versioned predictions
    pred_rows = list(
        (
            await session.execute(
                select(BreakoutPrediction)
                .where(BreakoutPrediction.post_id.in_(active_post_ids))
                .order_by(BreakoutPrediction.id.desc())
            )
        )
        .scalars()
        .all()
    )
    latest_pred_by_post: dict[int, BreakoutPrediction] = {}
    for pr in pred_rows:
        if pr.post_id not in latest_pred_by_post:
            latest_pred_by_post[pr.post_id] = pr

    assert algo_bundle.param_version_id is not None
    predictions_recorded = 0
    watch_count = 0
    surge_count = 0
    new_alerts_total = 0
    sse_emitted = 0
    wh_attempted = 0

    for p, scored in zip(active_posts, scored_list, strict=True):
        p.breakout_score = round(scored.breakout_score, 4)
        p.velocity_z_score = scored.velocity.velocity_z_score
        p.seismic_infectiousness = scored.seismic.infectiousness
        p.value_model_score = round(scored.value_model_score, 6)
        p.dpp_novelty_multiplier = scored.dpp_novelty_multiplier
        p.catalysis_boost = scored.catalysis_boost
        p.recommended_action = scored.recommended_action
        if not p.embedding_json:
            p.embedding_json = scored.embedding

        snap = newly_created_snap_by_post.get(p.id)
        if snap is not None:
            snap.breakout_score_at_capture = round(scored.breakout_score, 4)

        if scored.alert_tier == "surge":
            surge_count += 1
        elif scored.alert_tier == "watch":
            watch_count += 1

        prev_pred = latest_pred_by_post.get(p.id)
        bucket = select_snapshot_bucket(scored.age_minutes)
        should_record_pred = (
            prev_pred is None
            or prev_pred.alert_tier != scored.alert_tier
            or prev_pred.param_version_id != algo_bundle.param_version_id
            or select_snapshot_bucket(prev_pred.post_age_minutes) != bucket
        )
        if should_record_pred:
            session.add(
                BreakoutPrediction(
                    workspace_id=workspace.id,
                    post_id=p.id,
                    param_version_id=algo_bundle.param_version_id,
                    predicted_at=now_utc,
                    post_age_minutes=scored.age_minutes,
                    breakout_probability=round(scored.breakout_probability, 6),
                    breakout_score=round(scored.breakout_score, 4),
                    alert_tier=scored.alert_tier,
                )
            )
            predictions_recorded += 1

        if scored.alert_tier in ("watch", "surge"):
            author = accounts_by_id[p.author_account_id]
            alert_sum = await dispatch_breakout_alert(
                session,
                workspace,
                p,
                author,
                scored,
                now=now_utc,
                sse_hub=hub,
                retry_delays=retry_delays,
            )
            new_alerts_total += alert_sum.new_alerts_count
            if alert_sum.sse_delivered:
                sse_emitted += 1
            wh_attempted += len(alert_sum.webhook_outcomes)

    await session.commit()

    hub.publish(
        workspace.id,
        "radar.polled",
        {
            "polled_posts_count": len(fetched_by_x_id),
            "ingested_new_posts_count": ingested_new,
            "active_posts_count": len(active_posts),
            "watch_count": watch_count,
            "surge_count": surge_count,
            "new_alerts_count": new_alerts_total,
        },
    )
    sse_emitted += 1

    return RadarPollSummary(
        workspace_id=workspace.id,
        provider=provider.provider_name,
        polled_posts_count=len(fetched_by_x_id),
        ingested_new_posts_count=ingested_new,
        active_posts_count=len(active_posts),
        matured_now_count=matured_now,
        snapshots_upserted_count=snapshots_upserted,
        predictions_recorded_count=predictions_recorded,
        watch_count=watch_count,
        surge_count=surge_count,
        new_alerts_count=new_alerts_total,
        sse_events_emitted=sse_emitted,
        webhook_deliveries_attempted=wh_attempted,
        scored_results=scored_list,
    )
