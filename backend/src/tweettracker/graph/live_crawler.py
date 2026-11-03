"""Live X social graph crawler: wires seed-to-seed edges, @my_x_handle mutuals, and persistent 2-hop candidate staging."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import logging
import math
import re
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tweettracker.algo import PipelineParams
from tweettracker.models import (
    DiscoveredCandidate,
    PeerEndorsement,
    RelationshipEdge,
    TrackedAccount,
    Workspace,
)
from tweettracker.providers import (
    ProfileRecord,
    ProviderNotConfiguredError,
    SpendCapExceededError,
    estimate_workspace_monthly_units,
    get_x_data_provider,
)

logger = logging.getLogger("tweettracker.live_crawler")

_MENTION_RE = re.compile(r"@([A-Za-z0-9_]{1,32})")
FOLLOW_REFRESH_COOLDOWN = timedelta(hours=12)
COLD_START_FOLLOWER_CAP: int = PipelineParams().cold_start_follower_cap  # 50_000


def _ensure_utc(dt: datetime | None) -> datetime:
    if dt is None:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _norm_handle(raw: str | None) -> str:
    if not raw:
        return ""
    return raw.strip().lstrip("@").strip().lower()


async def crawl_live_seed_network(
    session: AsyncSession,
    workspace: Workspace,
    *,
    priority_account_ids: list[int] | None = None,
    throttle_mode: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Crawls live X following lists and recent conversational replies/quotes for a workspace:
    1. Resolves `followed_by_me` and `follows_me` relative to `workspace.my_x_handle`.
    2. Wires seed-to-seed `follow`, `reply`, and `quote` `RelationshipEdge` rows.
    3. Persists all 2-hop untracked peers (any follower count) into `DiscoveredCandidate` +
       deduplicated `PeerEndorsement` rows so bipartite evidence accumulates across syncs
       without fixed per-sync auto-add caps or follower-size cliffs.
    """
    if workspace.x_data_provider == "fixture":
        return {
            "crawled_seeds": 0,
            "edges_upserted": 0,
            "candidates_staged": 0,
            "endorsements_upserted": 0,
            "snowball_added_handles": [],
            "throttle_mode": "normal",
        }

    try:
        provider = get_x_data_provider(workspace.x_data_provider)
    except ProviderNotConfiguredError:
        return {
            "crawled_seeds": 0,
            "edges_upserted": 0,
            "candidates_staged": 0,
            "endorsements_upserted": 0,
            "snowball_added_handles": [],
            "throttle_mode": "normal",
        }

    now_utc = _ensure_utc(now)
    if throttle_mode is None:
        proj = await estimate_workspace_monthly_units(session, workspace, now=now_utc)
        eff_mode = str(proj.get("discovery_throttle_mode", "normal"))
    else:
        eff_mode = throttle_mode

    if eff_mode == "paused":
        return {
            "crawled_seeds": 0,
            "edges_upserted": 0,
            "candidates_staged": 0,
            "endorsements_upserted": 0,
            "snowball_added_handles": [],
            "throttle_mode": "paused",
        }

    priority_set = set(priority_account_ids or [])

    my_handle = _norm_handle(workspace.my_x_handle)

    all_accounts = list(
        (
            await session.execute(
                select(TrackedAccount).where(TrackedAccount.workspace_id == workspace.id)
            )
        )
        .scalars()
        .all()
    )
    active_accounts = [
        a for a in all_accounts if bool(a.is_tracked) and not bool(a.is_muted)
    ]
    if not active_accounts:
        return {
            "crawled_seeds": 0,
            "edges_upserted": 0,
            "candidates_staged": 0,
            "endorsements_upserted": 0,
            "snowball_added_handles": [],
            "throttle_mode": eff_mode,
        }

    muted_handles = {_norm_handle(a.handle) for a in all_accounts if bool(a.is_muted)}
    acct_by_handle = {_norm_handle(a.handle): a for a in active_accounts}

    existing_edges = list(
        (
            await session.execute(
                select(RelationshipEdge).where(RelationshipEdge.workspace_id == workspace.id)
            )
        )
        .scalars()
        .all()
    )
    edge_lookup: dict[tuple[int, int, str], RelationshipEdge] = {
        (e.source_account_id, e.target_account_id, e.edge_type): e for e in existing_edges
    }
    edges_upserted = 0

    def _upsert_edge(
        src_id: int,
        tgt_id: int,
        edge_type: str,
        weight_delta: float,
        event_at: datetime | None = None,
    ) -> None:
        nonlocal edges_upserted
        if src_id == tgt_id:
            return
        ev_utc = _ensure_utc(event_at) if event_at is not None else now_utc
        key = (src_id, tgt_id, edge_type)
        cur = edge_lookup.get(key)
        if cur is None:
            new_e = RelationshipEdge(
                workspace_id=workspace.id,
                source_account_id=src_id,
                target_account_id=tgt_id,
                edge_type=edge_type,
                interaction_count=1,
                decayed_weight=round(max(0.5, weight_delta), 3),
                last_event_at=ev_utc,
            )
            session.add(new_e)
            edge_lookup[key] = new_e
            edges_upserted += 1
        else:
            prev_ts = _ensure_utc(cur.last_event_at) if cur.last_event_at else None
            if edge_type != "follow" and (prev_ts is None or ev_utc > prev_ts):
                cur.interaction_count += 1
                cur.decayed_weight = round(min(40.0, cur.decayed_weight + weight_delta), 3)
            if prev_ts is None or ev_utc >= prev_ts:
                cur.last_event_at = ev_utc

    # 1. Sync `@my_x_handle` outbound following list so `followed_by_me` is accurate
    if my_handle:
        try:
            my_max_pages = 1 if eff_mode == "throttled" else 3
            my_following = await provider.get_following_list(
                session, workspace, my_handle, max_pages=my_max_pages, count=200
            )
            followed_handles = {_norm_handle(p.handle) for p in my_following if p.handle}
            for h, acct in acct_by_handle.items():
                if h in followed_handles:
                    acct.followed_by_me = True
        except (SpendCapExceededError, ProviderNotConfiguredError, httpx.HTTPError) as exc:
            logger.warning("Skipping my_x_handle following sync for @%s: %s", my_handle, exc)

    # 2. Dynamically pace following-list crawls across stale core drivers (12h cooldown),
    #    prioritizing proven high-yield accounts first, then active core drivers, then others.
    def _effective_yield(a: TrackedAccount) -> float:
        if int(a.reply_sample_count or 0) > 0 or float(a.yield_score or 0.0) > 0.0:
            return float(a.yield_score or 0.0)
        return float(a.core_affinity_score or 0.0)

    def _driver_tier(a: TrackedAccount) -> int:
        n_rep = int(a.reply_sample_count or 0)
        eff_y = _effective_yield(a)
        if n_rep >= 1 and eff_y >= 0.55:
            return 0  # Proven empirical reach winner (auto-discovered or seed)
        is_mut = bool(my_handle) and bool(a.follows_me and a.followed_by_me)
        if is_mut and eff_y >= 0.40:
            return 1
        if not bool(a.is_auto_tracked) and (
            (n_rep == 0 and (float(a.yield_score or 0.0) == 0.0 or eff_y >= 0.45))
            or eff_y >= 0.45
        ):
            return 1  # Active bootstrap seed
        return 2  # Untested auto-add or faded seed

    def _seed_sort_key(a: TrackedAccount) -> tuple[int, int, float, float]:
        in_prio = 0 if a.id in priority_set else 1
        tier = _driver_tier(a)
        last_ts = (
            _ensure_utc(a.last_follow_synced_at).timestamp()
            if a.last_follow_synced_at
            else 0.0
        )
        rank_score = max(
            _effective_yield(a) * 100.0,
            float(a.core_affinity_score or 0.0) * 100.0,
            float(a.heavy_hitter_score or 0.0),
        )
        return (in_prio, tier, last_ts, -rank_score)

    stale_seeds = [
        a
        for a in sorted(active_accounts, key=_seed_sort_key)
        if (
            a.id in priority_set
            or a.last_follow_synced_at is None
            or (now_utc - _ensure_utc(a.last_follow_synced_at)) >= FOLLOW_REFRESH_COOLDOWN
        )
    ]

    if priority_set:
        prio_only = [a for a in stale_seeds if a.id in priority_set]
        eligible_seeds = prio_only[:25] if prio_only else stale_seeds[:4]
    elif eff_mode == "throttled":
        eligible_seeds = stale_seeds[:3]
    else:
        batch_len = min(len(stale_seeds), max(4, math.ceil(len(stale_seeds) / 3))) if stale_seeds else 0
        eligible_seeds = stale_seeds[:batch_len]

    follow_max_pages = 1 if eff_mode == "throttled" else 2

    # Discovered 2-hop signals: handle -> list of (endorser_acct, signal_type, ProfileRecord | None, post_id | None, event_ts)
    discovered_signals: dict[
        str, list[tuple[TrackedAccount, str, ProfileRecord | None, str | None, datetime]]
    ] = defaultdict(list)
    crawled_seeds = 0

    for seed in eligible_seeds:
        try:
            following_profiles = await provider.get_following_list(
                session, workspace, seed.handle, max_pages=follow_max_pages, count=200
            )
        except SpendCapExceededError:
            break
        except (ProviderNotConfiguredError, httpx.HTTPError) as exc:
            logger.warning("Following fetch failed for @%s: %s", seed.handle, exc)
            continue

        seed.last_follow_synced_at = now_utc
        crawled_seeds += 1

        for prof in following_profiles:
            target_h = _norm_handle(prof.handle)
            if not target_h or target_h == _norm_handle(seed.handle):
                continue
            if my_handle and target_h == my_handle:
                seed.follows_me = True
                continue
            if target_h in acct_by_handle:
                peer = acct_by_handle[target_h]
                _upsert_edge(seed.id, peer.id, "follow", 2.0, now_utc)
            elif target_h not in muted_handles:
                discovered_signals[target_h].append((seed, "follow", prof, None, now_utc))

    # 3. Sample recent replies & quotes from core/eligible accounts using <=512-char packed queries
    if eligible_seeds:
        conv_Pool = eligible_seeds
    elif eff_mode == "normal":
        # Rotate conversational scan across yield-core drivers even when 12h follow lists are fresh
        drivers = [a for a in active_accounts if _driver_tier(a) <= 1] or list(active_accounts)
        conv_Pool = sorted(
            drivers,
            key=lambda a: (_driver_tier(a), -_effective_yield(a), a.id),
        )
    else:
        conv_Pool = []

    sample_handles = [a.handle for a in conv_Pool[:44]]
    if sample_handles:
        try:
            conv_posts = await provider.search_recent_posts(
                session,
                workspace,
                handles=sample_handles,
                exclude_replies=False,
            )
            for rec in conv_posts:
                src_acct = acct_by_handle.get(_norm_handle(rec.author_handle))
                if src_acct is None:
                    continue
                if not (rec.is_reply or rec.is_quote):
                    continue
                sig_type = "quote" if rec.is_quote else "reply"
                sig_w = 5.0
                rec_ts = _ensure_utc(rec.created_at_x) if rec.created_at_x else now_utc

                mentioned = {_norm_handle(m.group(1)) for m in _MENTION_RE.finditer(rec.text or "")}
                if rec.target_author_handle:
                    mentioned.add(_norm_handle(rec.target_author_handle))
                for target_h in mentioned:
                    if not target_h or target_h == _norm_handle(src_acct.handle):
                        continue
                    if my_handle and target_h == my_handle:
                        prev_i = _ensure_utc(src_acct.last_interaction_at) if src_acct.last_interaction_at else None
                        if prev_i is None or rec_ts > prev_i:
                            src_acct.last_interaction_at = rec_ts
                            src_acct.mutual_interaction_count_30d = int(
                                src_acct.mutual_interaction_count_30d or 0
                            ) + 1
                        continue
                    if target_h in acct_by_handle:
                        _upsert_edge(
                            src_acct.id,
                            acct_by_handle[target_h].id,
                            sig_type,
                            sig_w,
                            rec_ts,
                        )
                    elif target_h not in muted_handles:
                        discovered_signals[target_h].append(
                            (src_acct, sig_type, None, rec.x_post_id, rec_ts)
                        )
        except (SpendCapExceededError, ProviderNotConfiguredError, httpx.HTTPError) as exc:
            logger.warning("Conversational edge scan skipped: %s", exc)

    # 4. Persist all 2-hop candidates & deduplicated PeerEndorsement rows into DB
    candidates_staged = 0
    endorsements_upserted = 0
    if discovered_signals:
        existing_cands = list(
            (
                await session.execute(
                    select(DiscoveredCandidate).where(
                        DiscoveredCandidate.workspace_id == workspace.id
                    )
                )
            )
            .scalars()
            .all()
        )
        cand_by_handle: dict[str, DiscoveredCandidate] = {
            _norm_handle(c.handle): c for c in existing_cands
        }

        # Hydrate in 100-handle batches any @mentioned handle missing a ProfileRecord & not yet in DB
        missing_prof_handles = [
            h
            for h, items in discovered_signals.items()
            if h not in cand_by_handle
            and h not in acct_by_handle
            and h not in muted_handles
            and h != my_handle
            and all(it[2] is None for it in items)
        ]
        hydrated_extra: dict[str, ProfileRecord] = {}
        if missing_prof_handles and eff_mode != "paused":
            try:
                fetched_profs = await provider.get_profiles_by_handles(
                    session, workspace, missing_prof_handles
                )
                hydrated_extra = {_norm_handle(p.handle): p for p in fetched_profs if p.handle}
            except (SpendCapExceededError, ProviderNotConfiguredError, httpx.HTTPError):
                hydrated_extra = {}

        existing_ends = list(
            (
                await session.execute(
                    select(PeerEndorsement).where(PeerEndorsement.workspace_id == workspace.id)
                )
            )
            .scalars()
            .all()
        )
        end_by_tuple: dict[tuple[int, int, str], PeerEndorsement] = {
            (e.candidate_id, e.endorser_account_id, e.endorsement_type.strip().lower()): e
            for e in existing_ends
        }

        staged_batches: list[
            tuple[
                DiscoveredCandidate,
                list[tuple[TrackedAccount, str, ProfileRecord | None, str | None, datetime]],
            ]
        ] = []

        for cand_h, raw_items in discovered_signals.items():
            if cand_h in acct_by_handle or cand_h in muted_handles or cand_h == my_handle:
                continue

            best_prof: ProfileRecord | None = hydrated_extra.get(cand_h)
            for _, _, prof_obj, _, _ in raw_items:
                if prof_obj is not None:
                    best_prof = prof_obj
                    break

            cand_row = cand_by_handle.get(cand_h)
            if cand_row is None:
                if best_prof is None:
                    continue
                foll = max(0, int(best_prof.followers_count))
                foll_out = max(0, int(best_prof.following_count))
                init_status = "active" if foll < COLD_START_FOLLOWER_CAP else "over_cap"
                cand_row = DiscoveredCandidate(
                    workspace_id=workspace.id,
                    x_user_id=str(best_prof.x_user_id or cand_h),
                    handle=cand_h,
                    display_name=best_prof.display_name or cand_h,
                    bio=best_prof.bio or "",
                    avatar_url=best_prof.avatar_url,
                    followers_count=foll,
                    following_count=foll_out,
                    status=init_status,
                )
                session.add(cand_row)
                cand_by_handle[cand_h] = cand_row
                candidates_staged += 1
            else:
                if cand_row.status == "dismissed":
                    continue
                if best_prof is not None:
                    if best_prof.followers_count > 0:
                        cand_row.followers_count = int(best_prof.followers_count)
                    if best_prof.following_count > 0:
                        cand_row.following_count = int(best_prof.following_count)
                    if best_prof.display_name:
                        cand_row.display_name = best_prof.display_name
                    if best_prof.bio:
                        cand_row.bio = best_prof.bio
                    if best_prof.avatar_url:
                        cand_row.avatar_url = best_prof.avatar_url
                    if best_prof.x_user_id and not cand_row.x_user_id.isdigit():
                        cand_row.x_user_id = str(best_prof.x_user_id)
                # `pruned` is left alone: refresh_rising_candidates re-activates it only on
                # reply/quote evidence newer than `pruned_at`, not on a re-crawled follow.
                if cand_row.status == "promoted":
                    cand_row.status = (
                        "active"
                        if cand_row.followers_count < COLD_START_FOLLOWER_CAP
                        else "over_cap"
                    )
                elif cand_row.followers_count >= COLD_START_FOLLOWER_CAP and cand_row.status == "active":
                    cand_row.status = "over_cap"
                elif cand_row.followers_count < COLD_START_FOLLOWER_CAP and cand_row.status == "over_cap":
                    cand_row.status = "active"

            staged_batches.append((cand_row, raw_items))

        if candidates_staged > 0:
            await session.flush()

        for cand_row, raw_items in staged_batches:
            for endorser, stype, _, post_id, ev_ts in raw_items:
                norm_t = stype.strip().lower()
                tup = (cand_row.id, endorser.id, norm_t)
                existing_e = end_by_tuple.get(tup)
                if existing_e is None:
                    new_end = PeerEndorsement(
                        workspace_id=workspace.id,
                        candidate_id=cand_row.id,
                        endorser_account_id=endorser.id,
                        endorsement_type=norm_t,
                        source_x_post_id=post_id,
                        weight_contribution=1.0,
                        occurred_at=ev_ts,
                    )
                    session.add(new_end)
                    end_by_tuple[tup] = new_end
                    endorsements_upserted += 1
                elif norm_t in ("reply", "quote"):
                    prev_occ = _ensure_utc(existing_e.occurred_at)
                    if ev_ts > prev_occ or (post_id and post_id != existing_e.source_x_post_id):
                        existing_e.occurred_at = max(prev_occ, ev_ts)
                        if post_id:
                            existing_e.source_x_post_id = post_id

    await session.flush()
    return {
        "crawled_seeds": crawled_seeds,
        "edges_upserted": edges_upserted,
        "candidates_staged": candidates_staged,
        "endorsements_upserted": endorsements_upserted,
        "snowball_added_handles": [],
        "throttle_mode": eff_mode,
    }


async def refresh_tracked_profiles(session: AsyncSession, workspace: Workspace) -> int:
    """Re-hydrates follower counts and profile fields for every tracked account.

    Profiles are otherwise only fetched once at import, so counts go stale (or stay at 0 for
    accounts imported before a parser fix). Batched at 100 handles per 1-unit lookup.
    """
    if workspace.x_data_provider == "fixture":
        return 0
    try:
        provider = get_x_data_provider(workspace.x_data_provider)
    except ProviderNotConfiguredError:
        return 0

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
    if not accounts:
        return 0

    try:
        profiles = await provider.get_profiles_by_handles(
            session, workspace, [a.handle for a in accounts]
        )
    except (SpendCapExceededError, ProviderNotConfiguredError, httpx.HTTPError) as exc:
        logger.warning("Skipping tracked profile refresh for workspace %d: %s", workspace.id, exc)
        return 0

    prof_by_handle = {_norm_handle(p.handle): p for p in profiles if p.handle}
    updated = 0
    for acct in accounts:
        prof = prof_by_handle.get(_norm_handle(acct.handle))
        if prof is None or prof.followers_count <= 0:
            continue
        acct.followers_count = prof.followers_count
        acct.following_count = prof.following_count or acct.following_count
        acct.display_name = prof.display_name or acct.display_name
        acct.bio = prof.bio or acct.bio
        acct.avatar_url = prof.avatar_url or acct.avatar_url
        if prof.x_user_id and not acct.x_user_id.isdigit():
            acct.x_user_id = prof.x_user_id
        updated += 1
    return updated
