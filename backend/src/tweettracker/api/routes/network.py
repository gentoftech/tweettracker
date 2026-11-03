"""Epic 1 REST Routes: Curated Accounts, Bulk/CSV/List Import, WebGL Graph, Bridges & Mutual CRM."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
import hashlib
import io
import math
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
import httpx
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from tweettracker.algo import compute_text_embedding
from tweettracker.api.deps import get_db_session, require_admin_auth
from tweettracker.api.schemas import (
    AccountDetailOut,
    AccountImportRequest,
    AccountImportResultOut,
    AccountPostSummaryOut,
    AccountUpdateRequest,
    AutoSyncNetworkOut,
    CatalyzedConversationOut,
    CommunitySplitOut,
    CrossCommunityThreadOut,
    GraphEdgeOut,
    GraphNodeOut,
    GraphPoleOut,
    NetworkAccountOut,
    NetworkGraphOut,
    RecomputeOut,
    RelationshipRoiItemOut,
    RelationshipsOverviewOut,
    XYCoordOut,
    slugify,
)
from tweettracker.db import get_workspace_lock
from tweettracker.graph.bridges import _compute_core_periphery_positions
from tweettracker.graph.live_crawler import crawl_live_seed_network
from tweettracker.graph.service import (
    WorkspaceGraphAnalytics,
    build_analytics_from_orm,
    load_workspace_graph_inputs,
    recompute_workspace_graph,
)
from tweettracker.models import (
    AccountCommunityMembership,
    Community,
    DiscoveredCandidate,
    Post,
    PostInteraction,
    PostSnapshot,
    RelationshipEdge,
    TrackedAccount,
    Workspace,
)
from tweettracker.providers import (
    ProviderNotConfiguredError,
    SpendCapExceededError,
    get_x_data_provider,
)
from tweettracker.rising import auto_sync_workspace_network
from tweettracker.seed import _build_baseline_curves

router = APIRouter(
    prefix="/api/workspaces/{workspace_id}/network",
    tags=["network"],
    dependencies=[Depends(require_admin_auth)],
)

_X_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:x\.com|twitter\.com)/@?([A-Za-z0-9_]{1,32})(?:/|\?|$)",
    re.IGNORECASE,
)
_HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,32}$")
_RESERVED_X_PATHS = {
    "home",
    "explore",
    "search",
    "notifications",
    "messages",
    "i",
    "intent",
    "settings",
    "compose",
    "hashtag",
    "lists",
}


def extract_clean_handle(raw_token: str) -> str | None:
    """Extracts normalized lowercase X handle from `@handle`, `handle`, or `https://x.com/handle`."""
    token = raw_token.strip().strip("\"'")
    if not token:
        return None
    url_match = _X_URL_RE.search(token)
    if url_match:
        candidate = url_match.group(1).lower()
        return None if candidate in _RESERVED_X_PATHS else candidate

    stripped = token.lstrip("@").strip()
    if _HANDLE_RE.match(stripped) and stripped.lower() not in _RESERVED_X_PATHS:
        return stripped.lower()
    return None


def _resolve_community_tokens(
    tokens: list[str],
    communities: list[Community],
) -> set[int]:
    if not tokens or not communities:
        return set()
    by_name = {c.name.strip().lower(): c.id for c in communities}
    by_slug = {c.slug.strip().lower(): c.id for c in communities}
    by_id = {str(c.id): c.id for c in communities}

    matched: set[int] = set()
    for raw in tokens:
        for part in re.split(r"[|;/]+", raw):
            key = part.strip().lower()
            if not key:
                continue
            if key in by_name:
                matched.add(by_name[key])
            elif key in by_slug:
                matched.add(by_slug[key])
            elif slugify(key) in by_slug:
                matched.add(by_slug[slugify(key)])
            elif key in by_id:
                matched.add(by_id[key])
    return matched


def parse_import_entries(
    payload: AccountImportRequest,
    communities: list[Community],
) -> tuple[list[tuple[str, set[int]]], int]:
    """Parses handles_text, csv_text, and handles list into deduplicated `(handle, community_ids)`."""
    valid_comm_ids = {c.id for c in communities}
    default_cids = {cid for cid in payload.community_ids if cid in valid_comm_ids}

    ordered_handles: list[str] = []
    handle_comms: dict[str, set[int]] = {}
    duplicate_mentions = 0

    def _add(raw_h: str, extra_cids: set[int] | None = None) -> None:
        nonlocal duplicate_mentions
        clean = extract_clean_handle(raw_h)
        if not clean:
            return
        merged = set(default_cids)
        if extra_cids:
            merged.update(extra_cids)
        if clean in handle_comms:
            duplicate_mentions += 1
            handle_comms[clean].update(merged)
        else:
            ordered_handles.append(clean)
            handle_comms[clean] = merged

    for h in payload.handles:
        _add(h)

    if payload.csv_text and payload.csv_text.strip():
        reader = csv.reader(io.StringIO(payload.csv_text.strip()))
        for row_idx, row in enumerate(reader):
            if not row:
                continue
            first_col = row[0].strip()
            if not first_col or first_col.startswith("#"):
                continue
            if row_idx == 0 and first_col.lower() in (
                "handle",
                "username",
                "screen_name",
                "account",
                "user",
            ):
                continue
            row_comms = _resolve_community_tokens(row[1:], communities) if len(row) > 1 else set()
            _add(first_col, row_comms)

    if payload.handles_text and payload.handles_text.strip():
        for line in payload.handles_text.splitlines():
            stripped_line = line.strip()
            if not stripped_line or stripped_line.startswith("#"):
                continue
            # Check if line looks like CSV `handle,community_name` where col 2+ matches a community
            if "," in stripped_line:
                parts = [p.strip() for p in stripped_line.split(",") if p.strip()]
                if len(parts) >= 2:
                    maybe_comms = _resolve_community_tokens(parts[1:], communities)
                    if maybe_comms and extract_clean_handle(parts[0]):
                        _add(parts[0], maybe_comms)
                        continue
            # Otherwise split by commas / whitespace
            for tok in re.split(r"[\s,]+", stripped_line):
                if tok:
                    _add(tok)

    return [(h, handle_comms[h]) for h in ordered_handles], duplicate_mentions


def _serialize_network_account(
    acct: TrackedAccount,
    comms: list[Community],
    mem_by_acct_comm: dict[tuple[int, int], AccountCommunityMembership],
    analytics: WorkspaceGraphAnalytics,
    *,
    selected_community_id: int | None = None,
) -> NetworkAccountOut:
    u = acct.id
    hh_metrics = analytics.heavy_hitters.by_account.get(u)
    rel_metrics = analytics.relationships.by_account.get(u)

    splits: list[CommunitySplitOut] = []
    for c in comms:
        m = mem_by_acct_comm.get((u, c.id))
        is_man = bool(m.is_manual_tag) if m is not None else False
        kf = (
            float(m.nmf_known_for)
            if m is not None
            else float(analytics.simclusters.known_for.get(u, {}).get(c.id, 0.0))
        )
        inf_in = (
            float(m.nmf_interested_in)
            if m is not None
            else float(analytics.simclusters.interested_in.get(u, {}).get(c.id, 0.0))
        )
        splits.append(
            CommunitySplitOut(
                community_id=c.id,
                community_name=c.name,
                community_slug=c.slug,
                color=c.color,
                is_manual_tag=is_man,
                nmf_known_for=round(kf, 4),
                nmf_interested_in=round(inf_in, 4),
                pct=round(inf_in * 100.0, 1),
            )
        )

    prim_cid = analytics.simclusters.primary_community.get(u)
    prim_comm = next((c for c in comms if c.id == prim_cid), comms[0] if comms else None)

    comm_hh = acct.heavy_hitter_score
    if selected_community_id is not None and hh_metrics is not None:
        comm_hh = hh_metrics.community_scores.get(selected_community_id, acct.heavy_hitter_score)

    foll_eff = (
        hh_metrics.follower_efficiency
        if hh_metrics is not None
        else round(
            acct.heavy_hitter_score / max(1.0, math.log10(max(acct.followers_count, 100)) - 1.5),
            2,
        )
    )
    cat_cnt = hh_metrics.catalyzed_posts_count if hh_metrics is not None else 0
    is_mut_20x = (
        rel_metrics.is_mutual_20x
        if rel_metrics is not None
        else (bool(acct.follows_me and acct.followed_by_me) and acct.relationship_state != "none")
    )
    is_auto = bool(acct.is_auto_tracked)
    is_muted_flag = bool(acct.is_muted)
    core_aff = round(
        float(analytics.simclusters.core_affinity.get(u, acct.core_affinity_score or 0.0)),
        4,
    )
    ym = analytics.yield_by_account.get(u)
    yield_sc = round(
        float(ym.yield_score if ym is not None else (acct.yield_score or core_aff)),
        4,
    )
    rep_cnt = int(ym.reply_sample_count if ym is not None else (acct.reply_sample_count or 0))
    avg_rep_views = round(
        float(ym.avg_reply_views if ym is not None else (acct.avg_reply_views or 0.0)),
        1,
    )
    attr_folls = round(
        float(
            ym.attributed_follows_30d
            if ym is not None
            else (acct.attributed_follows_30d or 0.0)
        ),
        2,
    )
    inf_score = round(float(comm_hh), 1)
    if is_mut_20x:
        status_badge = "mutual"
    elif acct.relationship_state == "inbound_unreciprocated":
        status_badge = "follow_back"
    elif is_auto:
        status_badge = "rising" if acct.followers_count < 50_000 else "auto"
    else:
        status_badge = None

    auto_color = analytics.simclusters.auto_cluster_colors.get(
        u, prim_comm.color if prim_comm else "#38bdf8"
    )

    return NetworkAccountOut(
        id=acct.id,
        workspace_id=acct.workspace_id,
        x_user_id=acct.x_user_id,
        handle=acct.handle,
        display_name=acct.display_name or acct.handle,
        bio=acct.bio or "",
        avatar_url=acct.avatar_url,
        followers_count=acct.followers_count,
        following_count=acct.following_count,
        followers_7d_ago=acct.followers_7d_ago,
        follows_me=acct.follows_me,
        followed_by_me=acct.followed_by_me,
        relationship_state=acct.relationship_state,
        is_mutual_20x=is_mut_20x,
        mutual_interaction_count_30d=acct.mutual_interaction_count_30d,
        last_interaction_at=acct.last_interaction_at,
        heavy_hitter_score=acct.heavy_hitter_score,
        community_heavy_hitter_score=comm_hh,
        catalysis_lift=acct.catalysis_lift,
        catalyzed_posts_count=cat_cnt,
        s_core_depth=acct.s_core_depth,
        salsa_authority=acct.salsa_authority,
        salsa_scout=acct.salsa_scout,
        user_cred_v2=acct.user_cred_v2,
        follower_efficiency=foll_eff,
        bridge_score=acct.bridge_score,
        participation_coeff=acct.participation_coeff,
        burt_brokerage=acct.burt_brokerage,
        bridge_role=acct.bridge_role,
        sub_pocket_label=acct.sub_pocket_label,
        primary_community_id=(prim_comm.id if prim_comm else None),
        primary_community_name=(prim_comm.name if prim_comm else None),
        primary_community_color=auto_color,
        is_auto_tracked=is_auto,
        is_muted=is_muted_flag,
        core_affinity_score=core_aff,
        yield_score=yield_sc,
        reply_sample_count=rep_cnt,
        avg_reply_views=avg_rep_views,
        attributed_follows_30d=attr_folls,
        influence_score=inf_score,
        status_badge=status_badge,
        x_profile_url=f"https://x.com/{acct.handle}",
        community_splits=splits,
    )


async def _load_full_context(
    session: AsyncSession,
    workspace_id: int,
) -> tuple[
    Workspace,
    list[Community],
    list[TrackedAccount],
    dict[tuple[int, int], AccountCommunityMembership],
    list[RelationshipEdge],
    list[Post],
    list[PostInteraction],
    WorkspaceGraphAnalytics,
]:
    try:
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
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    analytics = build_analytics_from_orm(
        ws,
        comms,
        accts,
        memberships,
        rel_edges,
        posts,
        snaps_by_post,
        interactions,
    )
    mem_map = {(m.account_id, m.community_id): m for m in memberships}
    return ws, comms, accts, mem_map, rel_edges, posts, interactions, analytics


def _account_matches_community(
    acct_id: int,
    community_id: int,
    mem_map: dict[tuple[int, int], AccountCommunityMembership],
    analytics: WorkspaceGraphAnalytics,
) -> bool:
    m = mem_map.get((acct_id, community_id))
    if m is not None and (
        m.is_manual_tag
        or max(float(m.nmf_interested_in or 0.0), float(m.nmf_known_for or 0.0)) >= 0.25
    ):
        return True
    if analytics.simclusters.primary_community.get(acct_id) == community_id:
        return True
    if analytics.simclusters.interested_in.get(acct_id, {}).get(community_id, 0.0) >= 0.25:
        return True
    return False


@router.get("/accounts", response_model=list[NetworkAccountOut])
async def list_network_accounts(
    workspace_id: int,
    community_id: int | None = Query(default=None),
    bridge_role: str | None = Query(default=None),
    relationship_state: str | None = Query(default=None),
    rising_only: bool = Query(default=False),
    search: str | None = Query(default=None),
    sort_by: str = Query(default="heavy_hitter_score"),
    sort_dir: str = Query(default="desc"),
    session: AsyncSession = Depends(get_db_session),
) -> list[NetworkAccountOut]:
    _, comms, accts, mem_map, _, _, _, analytics = await _load_full_context(
        session, workspace_id
    )

    filtered: list[NetworkAccountOut] = []
    q = (search or "").strip().lower()

    for acct in accts:
        if rising_only and (not bool(acct.is_auto_tracked) or acct.followers_count >= 50_000):
            continue
        if community_id is not None and not _account_matches_community(
            acct.id, community_id, mem_map, analytics
        ):
            continue
        if bridge_role:
            br_filter = bridge_role.strip().lower()
            if br_filter == "bridges":
                if acct.bridge_role == "provincial":
                    continue
            elif acct.bridge_role != br_filter:
                continue
        if relationship_state and acct.relationship_state != relationship_state.strip().lower():
            continue
        if q:
            haystack = f"{acct.handle} {acct.display_name} {acct.bio} {acct.sub_pocket_label or ''}".lower()
            if q not in haystack:
                continue

        filtered.append(
            _serialize_network_account(
                acct,
                comms,
                mem_map,
                analytics,
                selected_community_id=community_id,
            )
        )

    reverse = sort_dir.strip().lower() != "asc"
    sort_key_name = sort_by.strip().lower()

    def _sort_key(item: NetworkAccountOut) -> tuple[float, int]:
        if sort_key_name == "heavy_hitter_score":
            val = (
                item.community_heavy_hitter_score
                if community_id is not None
                else item.heavy_hitter_score
            )
        elif sort_key_name == "catalysis_lift":
            val = item.catalysis_lift
        elif sort_key_name == "follower_efficiency":
            val = item.follower_efficiency
        elif sort_key_name == "bridge_score":
            val = item.bridge_score
        elif sort_key_name == "followers_count":
            val = float(item.followers_count)
        elif sort_key_name == "salsa_authority":
            val = item.salsa_authority
        elif sort_key_name == "user_cred_v2":
            val = item.user_cred_v2
        elif sort_key_name == "participation_coeff":
            val = item.participation_coeff
        elif sort_key_name == "burt_brokerage":
            val = item.burt_brokerage
        elif sort_key_name == "core_affinity_score":
            val = item.core_affinity_score
        elif sort_key_name == "yield_score":
            val = item.yield_score
        else:
            val = item.heavy_hitter_score
        return (val, -item.id if reverse else item.id)

    filtered.sort(key=_sort_key, reverse=reverse)
    return filtered


@router.post(
    "/accounts/import",
    response_model=AccountImportResultOut,
    status_code=status.HTTP_201_CREATED,
)
async def import_network_accounts(
    workspace_id: int,
    payload: AccountImportRequest,
    session: AsyncSession = Depends(get_db_session),
) -> AccountImportResultOut:
    async with get_workspace_lock(workspace_id):
        ws = (
            await session.execute(select(Workspace).where(Workspace.id == workspace_id))
        ).scalar_one_or_none()
        if ws is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Workspace {workspace_id} not found.",
            )

        comms = list(
            (
                await session.execute(
                    select(Community)
                    .where(Community.workspace_id == workspace_id)
                    .order_by(Community.sort_order.asc(), Community.id.asc())
                )
            ).scalars().all()
        )
        valid_comm_ids = {c.id for c in comms}

        try:
            provider = get_x_data_provider(ws.x_data_provider)
        except ProviderNotConfiguredError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

        parsed_entries, batch_dup_cnt = parse_import_entries(payload, comms)
        entry_map: dict[str, set[int]] = {h: set(cids) for h, cids in parsed_entries}
        ordered_handles: list[str] = [h for h, _ in parsed_entries]

        # Optional X List sync via workspace's active XDataProvider
        if payload.x_list_id and payload.x_list_id.strip():
            lid = payload.x_list_id.strip()
            try:
                list_posts = await provider.search_recent_posts(
                    session, ws, list_id=lid, exclude_replies=True
                )
            except (ProviderNotConfiguredError, SpendCapExceededError) as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
                ) from exc
            except httpx.HTTPError as exc:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="Upstream X data provider request failed.",
                ) from exc

            list_matched_comms = {c.id for c in comms if c.x_list_id == lid}
            default_for_list = list_matched_comms or {
                cid for cid in payload.community_ids if cid in valid_comm_ids
            }
            for p_rec in list_posts:
                clean_h = extract_clean_handle(p_rec.author_handle)
                if not clean_h:
                    continue
                if clean_h in entry_map:
                    entry_map[clean_h].update(default_for_list)
                else:
                    ordered_handles.append(clean_h)
                    entry_map[clean_h] = set(default_for_list)

        if not ordered_handles:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No valid @handles, X URLs, CSV rows, or X List members provided.",
            )

        # Fallback default community if workspace has communities and none were tagged
        fallback_cid = comms[0].id if comms else None

        existing_accts = list(
            (
                await session.execute(
                    select(TrackedAccount).where(TrackedAccount.workspace_id == workspace_id)
                )
            ).scalars().all()
        )
        existing_by_handle = {a.handle.lower(): a for a in existing_accts}

        new_handles = [h for h in ordered_handles if h not in existing_by_handle]
        updated_existing_count = 0
        deduplicated_total = batch_dup_cnt

        # Hydrate new profiles via active XDataProvider
        hydrated_by_handle = {}
        if new_handles:
            try:
                prof_records = await provider.get_profiles_by_handles(session, ws, new_handles)
            except (ProviderNotConfiguredError, SpendCapExceededError) as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
                ) from exc
            except httpx.HTTPError as exc:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="Upstream X data provider request failed.",
                ) from exc
            for pr in prof_records:
                ch = extract_clean_handle(pr.handle) or pr.handle.lower()
                hydrated_by_handle[ch] = pr

        existing_mem_rows = list(
            (
                await session.execute(
                    select(AccountCommunityMembership).where(
                        AccountCommunityMembership.workspace_id == workspace_id
                    )
                )
            ).scalars().all()
        )
        mem_lookup = {(m.account_id, m.community_id): m for m in existing_mem_rows}

        newly_created_accts: list[tuple[TrackedAccount, set[int]]] = []
        touched_ids: list[int] = []
        now = datetime.now(timezone.utc)
        is_fixture_mode = ws.x_data_provider == "fixture"

        for h in ordered_handles:
            target_cids = set(entry_map.get(h, set()))
            if not target_cids and fallback_cid is not None:
                target_cids.add(fallback_cid)

            if h in existing_by_handle:
                acct = existing_by_handle[h]
                was_tracked = bool(acct.is_tracked) and not bool(acct.is_muted)
                acct.is_tracked = True
                acct.is_muted = False
                acct.is_auto_tracked = False
                acct.auto_tracked_at = None
                updated_existing_count += 1
                if was_tracked:
                    deduplicated_total += 1
                touched_ids.append(acct.id)
                for cid in target_cids:
                    m_row = mem_lookup.get((acct.id, cid))
                    if m_row is not None:
                        m_row.is_manual_tag = True
                    else:
                        new_m = AccountCommunityMembership(
                            workspace_id=workspace_id,
                            account_id=acct.id,
                            community_id=cid,
                            is_manual_tag=True,
                            nmf_known_for=1.0,
                            nmf_interested_in=1.0 / max(1, len(target_cids)),
                        )
                        session.add(new_m)
                        mem_lookup[(acct.id, cid)] = new_m
            else:
                prof = hydrated_by_handle.get(h)
                if not is_fixture_mode and prof is None:
                    # Live X provider returned no matching profile: never fabricate ghost accounts
                    continue

                if is_fixture_mode:
                    digest = int.from_bytes(
                        hashlib.blake2b(h.encode("utf-8"), digest_size=4).digest(), "little"
                    )
                    followers = (
                        prof.followers_count
                        if (prof and prof.followers_count > 0)
                        else (3_500 + (digest % 42_000))
                    )
                    following = (
                        prof.following_count
                        if (prof and prof.following_count > 0)
                        else (320 + (digest % 900))
                    )
                    x_uid = (
                        prof.x_user_id
                        if (prof and prof.x_user_id)
                        else str(900_000_000 + (digest % 90_000_000))
                    )
                    has_my_handle = bool((ws.my_x_handle or "").strip())
                    follows_me = bool(has_my_handle and (digest % 5 in (0, 1)))
                    followed_by_me = bool(has_my_handle and (digest % 5 in (0, 2, 3)))
                    foll_7d = max(50, int(followers * 0.97))
                    last_inter = (
                        now - timedelta(days=3) if (has_my_handle and digest % 3 == 0) else None
                    )
                    mut_cnt_30d = 2 + (digest % 5) if (has_my_handle and digest % 3 == 0) else 0
                    disp_name = (
                        prof.display_name
                        if prof and prof.display_name
                        else h.replace("_", " ").title()
                    )
                    bio_txt = prof.bio if prof and prof.bio else f"Curated ecosystem voice @{h}."
                    b_curves = _build_baseline_curves(max(600, int(followers * 0.16)))
                else:
                    assert prof is not None
                    followers = max(0, int(prof.followers_count))
                    following = max(0, int(prof.following_count))
                    x_uid = str(prof.x_user_id or h)
                    follows_me = False
                    followed_by_me = False
                    foll_7d = followers
                    last_inter = None
                    mut_cnt_30d = 0
                    disp_name = prof.display_name or h
                    bio_txt = prof.bio or ""
                    b_curves = (
                        _build_baseline_curves(max(100, int(followers * 0.16)))
                        if followers > 0
                        else {}
                    )

                new_acct = TrackedAccount(
                    workspace_id=workspace_id,
                    x_user_id=x_uid,
                    handle=h,
                    display_name=disp_name,
                    bio=bio_txt,
                    avatar_url=(prof.avatar_url if prof else None),
                    followers_count=followers,
                    following_count=following,
                    followers_7d_ago=foll_7d,
                    is_tracked=True,
                    follows_me=follows_me,
                    followed_by_me=followed_by_me,
                    relationship_state="none",
                    last_interaction_at=last_inter,
                    mutual_interaction_count_30d=mut_cnt_30d,
                    baseline_curves_json=b_curves,
                )
                session.add(new_acct)
                await session.flush()

                existing_by_handle[h] = new_acct
                newly_created_accts.append((new_acct, target_cids))
                touched_ids.append(new_acct.id)

                for cid in target_cids:
                    new_m = AccountCommunityMembership(
                        workspace_id=workspace_id,
                        account_id=new_acct.id,
                        community_id=cid,
                        is_manual_tag=True,
                        nmf_known_for=1.0,
                        nmf_interested_in=1.0 / max(1, len(target_cids)),
                    )
                    session.add(new_m)
                    mem_lookup[(new_acct.id, cid)] = new_m

        if not touched_ids:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="None of the requested handles could be resolved from the live X data provider.",
            )

        # In offline FixtureProvider mode, synthesize deterministic edges & sample post so UI testing works smoothly
        if is_fixture_mode and newly_created_accts:
            all_ws_accts = list(existing_by_handle.values())
            acct_comm_tags: dict[int, set[int]] = {}
            for (aid, cid), m_obj in mem_lookup.items():
                if m_obj.is_manual_tag:
                    acct_comm_tags.setdefault(aid, set()).add(cid)

            existing_edge_tuples = set(
                (
                    await session.execute(
                        select(
                            RelationshipEdge.source_account_id,
                            RelationshipEdge.target_account_id,
                            RelationshipEdge.edge_type,
                        ).where(RelationshipEdge.workspace_id == workspace_id)
                    )
                ).all()
            )

            for new_acct, target_cids in newly_created_accts:
                digest = int.from_bytes(
                    hashlib.blake2b(new_acct.handle.encode("utf-8"), digest_size=4).digest(),
                    "little",
                )
                for cid in target_cids:
                    peers = [
                        p
                        for p in all_ws_accts
                        if p.id != new_acct.id and cid in acct_comm_tags.get(p.id, set())
                    ]
                    if not peers:
                        peers = [p for p in all_ws_accts if p.id != new_acct.id]
                    for p_off in range(min(3, len(peers))):
                        peer = peers[(digest + p_off * 3) % len(peers)]
                        etype = "reply" if p_off == 0 else ("quote" if p_off == 1 else "follow")
                        tup_out = (new_acct.id, peer.id, etype)
                        if tup_out not in existing_edge_tuples:
                            existing_edge_tuples.add(tup_out)
                            session.add(
                                RelationshipEdge(
                                    workspace_id=workspace_id,
                                    source_account_id=new_acct.id,
                                    target_account_id=peer.id,
                                    edge_type=etype,
                                    interaction_count=2 + (p_off % 3),
                                    decayed_weight=4.5,
                                    last_event_at=now - timedelta(days=1 + p_off),
                                )
                            )
                        tup_in = (peer.id, new_acct.id, "follow")
                        if tup_in not in existing_edge_tuples:
                            existing_edge_tuples.add(tup_in)
                            session.add(
                                RelationshipEdge(
                                    workspace_id=workspace_id,
                                    source_account_id=peer.id,
                                    target_account_id=new_acct.id,
                                    edge_type="follow",
                                    interaction_count=1,
                                    decayed_weight=2.0,
                                    last_event_at=now - timedelta(days=2),
                                )
                            )

                # Synthesize 1 sample post for the newly imported fixture voice
                x_pid = f"fx_{workspace_id}_{new_acct.id}_{digest % 1_000_000}"
                views = max(900, int(new_acct.followers_count * 0.24))
                sample_txt = f"Notes from @{new_acct.handle}: shipping high-leverage execution loops across our ecosystem."
                synth_post = Post(
                    workspace_id=workspace_id,
                    x_post_id=x_pid,
                    author_account_id=new_acct.id,
                    text=sample_txt,
                    created_at_x=now - timedelta(minutes=34),
                    is_reply=False,
                    is_retweet=False,
                    is_quote=False,
                    embedding_json=compute_text_embedding(sample_txt),
                    latest_views=views,
                    latest_likes=int(views * 0.022),
                    latest_replies=int(views * 0.004),
                    latest_quotes=int(views * 0.0018),
                    latest_reposts=int(views * 0.0025),
                    latest_bookmarks=int(views * 0.006),
                    breakout_score=68.5,
                    velocity_z_score=1.8,
                    seismic_infectiousness=0.52,
                    value_model_score=0.84,
                    dpp_novelty_multiplier=0.95,
                    catalysis_boost=0.14,
                    recommended_action="watch",
                    matured_48h=False,
                )
                session.add(synth_post)
                await session.flush()
                session.add(
                    PostSnapshot(
                        post_id=synth_post.id,
                        age_minutes=30,
                        captured_at=now - timedelta(minutes=4),
                        views=int(views * 0.88),
                        likes=int(views * 0.019),
                        replies=int(views * 0.003),
                        quotes=int(views * 0.0015),
                        reposts=int(views * 0.002),
                        bookmarks=int(views * 0.005),
                        breakout_score_at_capture=66.0,
                    )
                )
        elif not is_fixture_mode and touched_ids:
            await session.flush()
            # Pure manual import: wire seed edges + @my_x_handle mutuals and stage 2-hop
            # peers in DiscoveredCandidate without silently auto-adding unrequested accounts.
            await crawl_live_seed_network(
                session,
                ws,
                priority_account_ids=touched_ids,
                now=now,
            )

        await session.flush()

        compute_ms = 0.0
        if payload.recompute:
            summary = await recompute_workspace_graph(session, workspace_id, commit=True)
            compute_ms = summary.compute_duration_ms
        else:
            await session.commit()

        _, comms_after, accts_after, mem_after, _, _, _, analytics_after = await _load_full_context(
            session, workspace_id
        )
        touched_set = set(touched_ids)
        imported_serialized = [
            _serialize_network_account(a, comms_after, mem_after, analytics_after)
            for a in accts_after
            if a.id in touched_set
        ]

        return AccountImportResultOut(
            workspace_id=workspace_id,
            imported_count=len(newly_created_accts),
            updated_existing_count=updated_existing_count,
            deduplicated_count=deduplicated_total,
            total_tracked_accounts=len(accts_after),
            compute_duration_ms=compute_ms,
            accounts=imported_serialized,
        )


@router.patch("/accounts/{account_id}", response_model=NetworkAccountOut)
async def update_network_account(
    workspace_id: int,
    account_id: int,
    payload: AccountUpdateRequest,
    session: AsyncSession = Depends(get_db_session),
) -> NetworkAccountOut:
    async with get_workspace_lock(workspace_id):
        acct = (
            await session.execute(
                select(TrackedAccount).where(
                    TrackedAccount.id == account_id,
                    TrackedAccount.workspace_id == workspace_id,
                )
            )
        ).scalar_one_or_none()
        if acct is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"TrackedAccount {account_id} not found in workspace {workspace_id}.",
            )

        fields = payload.model_fields_set
        if "followed_by_me" in fields and payload.followed_by_me is not None:
            acct.followed_by_me = payload.followed_by_me
        if "follows_me" in fields and payload.follows_me is not None:
            acct.follows_me = payload.follows_me
        if "is_tracked" in fields and payload.is_tracked is not None:
            acct.is_tracked = payload.is_tracked
        if "is_muted" in fields and payload.is_muted is not None:
            acct.is_muted = payload.is_muted
            if payload.is_muted:
                acct.is_tracked = False
                cand_rows = list(
                    (
                        await session.execute(
                            select(DiscoveredCandidate).where(
                                DiscoveredCandidate.workspace_id == workspace_id,
                                func.lower(DiscoveredCandidate.handle) == acct.handle.lower(),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                for cr in cand_rows:
                    cr.status = "dismissed"
        if "display_name" in fields and payload.display_name is not None:
            acct.display_name = payload.display_name.strip()
        if "bio" in fields and payload.bio is not None:
            acct.bio = payload.bio

        if "community_ids" in fields and payload.community_ids is not None:
            valid_cids = set(
                (
                    await session.execute(
                        select(Community.id).where(Community.workspace_id == workspace_id)
                    )
                ).scalars().all()
            )
            chosen_cids = {cid for cid in payload.community_ids if cid in valid_cids}
            mem_rows = list(
                (
                    await session.execute(
                        select(AccountCommunityMembership).where(
                            AccountCommunityMembership.workspace_id == workspace_id,
                            AccountCommunityMembership.account_id == account_id,
                        )
                    )
                ).scalars().all()
            )
            by_cid = {m.community_id: m for m in mem_rows}
            for cid in valid_cids:
                is_man = cid in chosen_cids
                if cid in by_cid:
                    by_cid[cid].is_manual_tag = is_man
                elif is_man:
                    session.add(
                        AccountCommunityMembership(
                            workspace_id=workspace_id,
                            account_id=account_id,
                            community_id=cid,
                            is_manual_tag=True,
                            nmf_known_for=1.0,
                            nmf_interested_in=1.0 / max(1, len(chosen_cids)),
                        )
                    )

        await session.flush()
        await recompute_workspace_graph(session, workspace_id, commit=True)

        _, comms, accts, mem_map, _, _, _, analytics = await _load_full_context(
            session, workspace_id
        )
        updated = next((a for a in accts if a.id == account_id), None)
        if updated is None:
            await session.refresh(acct)
            return _serialize_network_account(acct, comms, mem_map, analytics)
        return _serialize_network_account(updated, comms, mem_map, analytics)


@router.post("/auto-sync", response_model=AutoSyncNetworkOut)
async def auto_sync_network_endpoint(
    workspace_id: int,
    session: AsyncSession = Depends(get_db_session),
) -> AutoSyncNetworkOut:
    """Syncs network via SimClusters core affinity + UserCredV2 thresholds and hysteresis pruning."""
    async with get_workspace_lock(workspace_id):
        ws_exists = (
            await session.execute(select(Workspace.id).where(Workspace.id == workspace_id))
        ).scalar_one_or_none()
        if ws_exists is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Workspace {workspace_id} not found.",
            )
        summary = await auto_sync_workspace_network(session, workspace_id)
        return AutoSyncNetworkOut(**summary)


@router.delete("/accounts/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_network_account(
    workspace_id: int,
    account_id: int,
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    async with get_workspace_lock(workspace_id):
        acct = (
            await session.execute(
                select(TrackedAccount).where(
                    TrackedAccount.id == account_id,
                    TrackedAccount.workspace_id == workspace_id,
                )
            )
        ).scalar_one_or_none()
        if acct is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"TrackedAccount {account_id} not found in workspace {workspace_id}.",
            )

        await session.execute(
            delete(RelationshipEdge).where(
                RelationshipEdge.workspace_id == workspace_id,
                or_(
                    RelationshipEdge.source_account_id == account_id,
                    RelationshipEdge.target_account_id == account_id,
                ),
            )
        )
        await session.delete(acct)
        await session.flush()
        await recompute_workspace_graph(session, workspace_id, commit=True)
        return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/recompute", response_model=RecomputeOut)
async def recompute_network_endpoint(
    workspace_id: int,
    session: AsyncSession = Depends(get_db_session),
) -> RecomputeOut:
    async with get_workspace_lock(workspace_id):
        ws_exists = (
            await session.execute(select(Workspace.id).where(Workspace.id == workspace_id))
        ).scalar_one_or_none()
        if ws_exists is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Workspace {workspace_id} not found.",
            )

        summary = await recompute_workspace_graph(session, workspace_id, commit=True)
        return RecomputeOut(
            workspace_id=summary.workspace_id,
            communities_count=summary.communities_count,
            accounts_count=summary.accounts_count,
            edges_count=summary.edges_count,
            bridges_count=summary.bridges_count,
            mutual_active_count=summary.mutual_active_count,
            compute_duration_ms=summary.compute_duration_ms,
        )


@router.get("/graph", response_model=NetworkGraphOut)
async def get_network_graph(
    workspace_id: int,
    mode: str = Query(default="community"),
    community_id: int | None = Query(default=None),
    max_edges: int = Query(default=350, ge=20, le=2500),
    session: AsyncSession = Depends(get_db_session),
) -> NetworkGraphOut:
    ws, comms, accts, mem_map, _, _, _, analytics = await _load_full_context(
        session, workspace_id
    )
    k = len(comms)
    clean_mode = "bridges" if (mode.strip().lower() == "bridges" and k >= 2) else "community"

    selected_cid = community_id if clean_mode == "community" else None
    if selected_cid is not None and all(c.id != selected_cid for c in comms):
        selected_cid = comms[0].id if comms else None

    if selected_cid is not None:
        visible_accts = [
            a for a in accts if _account_matches_community(a.id, selected_cid, mem_map, analytics)
        ]
    else:
        visible_accts = list(accts)

    visible_ids = [a.id for a in visible_accts]
    visible_set = set(visible_ids)

    focused_single_xy: dict[int, tuple[float, float]] = {}
    if selected_cid is not None:
        hh_subset = {
            a.id: (
                analytics.heavy_hitters.by_account[a.id].community_scores.get(
                    selected_cid, a.heavy_hitter_score
                )
                if a.id in analytics.heavy_hitters.by_account
                else a.heavy_hitter_score
            )
            for a in visible_accts
        }
        sub_lbl_subset = {a.id: (a.sub_pocket_label or "Core #1") for a in visible_accts}
        focused_single_xy = _compute_core_periphery_positions(
            visible_ids, hh_subset, sub_lbl_subset
        )

    comm_counts: dict[int, int] = {c.id: 0 for c in comms}
    for a in accts:
        for c in comms:
            if _account_matches_community(a.id, c.id, mem_map, analytics):
                comm_counts[c.id] += 1

    poles_out = [
        GraphPoleOut(
            community_id=p.community_id,
            name=p.name,
            slug=p.slug,
            color=p.color,
            x=p.x,
            y=p.y,
            accounts_count=comm_counts.get(p.community_id, 0),
        )
        for p in analytics.bridges.poles
    ]

    selected_comm_obj = next((c for c in comms if c.id == selected_cid), None)

    nodes_out: list[GraphNodeOut] = []
    for acct in visible_accts:
        ser = _serialize_network_account(
            acct, comms, mem_map, analytics, selected_community_id=selected_cid
        )
        br_info = analytics.bridges.by_account.get(acct.id)
        np_xy = br_info.n_pole_xy if br_info else (0.0, 0.0)
        if selected_cid is not None:
            sc_xy = focused_single_xy.get(
                acct.id, br_info.single_community_xy if br_info else (0.0, 0.0)
            )
        else:
            sc_xy = np_xy

        if selected_comm_obj is not None and acct.bridge_role == "provincial":
            node_color = selected_comm_obj.color
        else:
            node_color = analytics.simclusters.auto_cluster_colors.get(
                acct.id, ser.primary_community_color
            )

        nodes_out.append(
            GraphNodeOut(
                id=acct.id,
                handle=acct.handle,
                display_name=ser.display_name,
                followers_count=acct.followers_count,
                single_community_xy=XYCoordOut(x=sc_xy[0], y=sc_xy[1]),
                n_pole_xy=XYCoordOut(x=np_xy[0], y=np_xy[1]),
                color=node_color,
                primary_community_id=ser.primary_community_id,
                primary_community_name=ser.primary_community_name,
                heavy_hitter_score=ser.heavy_hitter_score,
                community_heavy_hitter_score=ser.community_heavy_hitter_score,
                catalysis_lift=ser.catalysis_lift,
                catalyzed_posts_count=ser.catalyzed_posts_count,
                s_core_depth=ser.s_core_depth,
                salsa_authority=ser.salsa_authority,
                salsa_scout=ser.salsa_scout,
                user_cred_v2=ser.user_cred_v2,
                follower_efficiency=ser.follower_efficiency,
                bridge_score=ser.bridge_score,
                participation_coeff=ser.participation_coeff,
                burt_brokerage=ser.burt_brokerage,
                bridge_role=ser.bridge_role,
                relationship_state=ser.relationship_state,
                is_mutual_20x=ser.is_mutual_20x,
                follows_me=ser.follows_me,
                followed_by_me=ser.followed_by_me,
                is_auto_tracked=ser.is_auto_tracked,
                is_muted=ser.is_muted,
                core_affinity_score=ser.core_affinity_score,
                yield_score=ser.yield_score,
                reply_sample_count=ser.reply_sample_count,
                avg_reply_views=ser.avg_reply_views,
                attributed_follows_30d=ser.attributed_follows_30d,
                influence_score=ser.influence_score,
                status_badge=ser.status_badge,
                sub_pocket_label=ser.sub_pocket_label,
                community_mix=ser.community_splits,
            )
        )

    # Filter behavioral edges cleanly: keep strongest edge per node + top remaining by weight
    prim_comm_by_id = analytics.simclusters.primary_community
    candidate_edges: list[tuple[float, int, int, str, bool, bool]] = []
    seen_undir: dict[tuple[int, int], tuple[float, int, int, str, bool, bool]] = {}

    for u, v, data in analytics.bundle.directed.edges(data=True):
        if u not in visible_set or v not in visible_set:
            continue
        w = float(data.get("weight", 0.0))
        if w <= 0.0:
            continue
        etype = str(data.get("edge_type", "follow"))
        is_mut = bool(data.get("is_mutual_follow", False))
        cross_c = prim_comm_by_id.get(u) != prim_comm_by_id.get(v)
        pair_key = (u, v) if u < v else (v, u)
        prev = seen_undir.get(pair_key)
        if prev is None:
            seen_undir[pair_key] = (w, u, v, etype, is_mut, cross_c)
        else:
            # Combine bidirectional behavioral weight into crisp single WebGL line
            comb_w = prev[0] + w
            best_type = etype if w > prev[0] else prev[3]
            seen_undir[pair_key] = (comb_w, prev[1], prev[2], best_type, is_mut or prev[4], cross_c)

    candidate_edges = sorted(seen_undir.values(), key=lambda x: (-x[0], x[1], x[2]))

    chosen_edges: list[tuple[float, int, int, str, bool, bool]] = []
    chosen_keys: set[tuple[int, int]] = set()
    covered_nodes: set[int] = set()

    # Pass 1: guarantee each connected node keeps its strongest behavioral tie
    for item in candidate_edges:
        if len(chosen_edges) >= max_edges:
            break
        _, u, v, _, _, _ = item
        if u not in covered_nodes or v not in covered_nodes:
            chosen_edges.append(item)
            chosen_keys.add((u, v))
            covered_nodes.add(u)
            covered_nodes.add(v)

    # Pass 2: fill remaining budget with highest-weight ties (prioritizing cross-community in bridge mode)
    remaining = [it for it in candidate_edges if (it[1], it[2]) not in chosen_keys]
    if clean_mode == "bridges":
        remaining.sort(key=lambda it: (0 if it[5] else 1, -it[0], it[1]))

    for item in remaining:
        if len(chosen_edges) >= max_edges:
            break
        chosen_edges.append(item)

    edges_out = [
        GraphEdgeOut(
            id=f"e-{u}-{v}",
            source=u,
            target=v,
            weight=round(w, 3),
            edge_type=etype,
            is_mutual=is_mut,
            cross_community=cross_c,
        )
        for w, u, v, etype, is_mut, cross_c in chosen_edges
    ]

    return NetworkGraphOut(
        workspace_id=ws.id,
        mode=clean_mode,
        k_communities=k,
        bridge_map_available=(k >= 2),
        has_own_handle=analytics.relationships.has_own_handle,
        my_x_handle=analytics.relationships.my_x_handle,
        selected_community_id=selected_cid,
        poles=poles_out,
        nodes=nodes_out,
        edges=edges_out,
    )


@router.get("/accounts/{account_id}/detail", response_model=AccountDetailOut)
async def get_network_account_detail(
    workspace_id: int,
    account_id: int,
    session: AsyncSession = Depends(get_db_session),
) -> AccountDetailOut:
    ws, comms, accts, mem_map, _, posts, interactions, analytics = await _load_full_context(
        session, workspace_id
    )
    acct = next((a for a in accts if a.id == account_id), None)
    if acct is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"TrackedAccount {account_id} not found in workspace {workspace_id}.",
        )

    acct_out = _serialize_network_account(acct, comms, mem_map, analytics)
    accts_by_id = {a.id: a for a in accts}
    posts_by_id = {p.id: p for p in posts}
    comm_by_id = {c.id: c for c in comms}

    # Catalyzed conversations where this account's reply/quote preceded velocity lift
    cat_event_map = dict(analytics.heavy_hitters.catalyzed_events.get(account_id, []))
    catalyzed_list: list[CatalyzedConversationOut] = []
    seen_c_posts: set[int] = set()

    for pi in sorted(
        interactions,
        key=lambda x: x.occurred_at or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    ):
        if pi.actor_account_id != account_id:
            continue
        if pi.post_id in seen_c_posts:
            continue
        p_obj = posts_by_id.get(pi.post_id)
        if p_obj is None or p_obj.author_account_id == account_id:
            continue
        seen_c_posts.add(pi.post_id)
        author_obj = accts_by_id.get(p_obj.author_account_id)
        author_h = author_obj.handle if author_obj else "unknown"
        author_disp = (author_obj.display_name or author_h) if author_obj else author_h
        lift_val = cat_event_map.get(p_obj.id, acct.catalysis_lift)

        catalyzed_list.append(
            CatalyzedConversationOut(
                post_id=p_obj.id,
                x_post_id=p_obj.x_post_id,
                author_handle=author_h,
                author_display_name=author_disp,
                text=p_obj.text,
                interaction_type=pi.interaction_type,
                occurred_at=pi.occurred_at,
                latest_views=p_obj.latest_views,
                observed_lift=round(float(lift_val), 2),
                breakout_score=p_obj.breakout_score,
                x_post_url=f"https://x.com/{author_h}/status/{p_obj.x_post_id}",
            )
        )

    catalyzed_list.sort(key=lambda c: (-c.observed_lift, -c.latest_views))

    # Account's own recent original/quote posts
    own_posts = [
        p
        for p in posts
        if p.author_account_id == account_id and not p.is_reply and not p.is_retweet
    ]
    own_posts.sort(key=lambda p: p.created_at_x, reverse=True)
    recent_posts_out = [
        AccountPostSummaryOut(
            post_id=p.id,
            x_post_id=p.x_post_id,
            text=p.text,
            created_at_x=p.created_at_x,
            latest_views=p.latest_views,
            latest_likes=p.latest_likes,
            latest_replies=p.latest_replies,
            latest_quotes=p.latest_quotes,
            value_model_score=p.value_model_score,
            breakout_score=p.breakout_score,
            is_quote=p.is_quote,
            x_post_url=f"https://x.com/{acct.handle}/status/{p.x_post_id}",
        )
        for p in own_posts[:8]
    ]

    # Cross-community threads / ties
    my_prim_c = analytics.simclusters.primary_community.get(account_id)
    cross_ties: list[CrossCommunityThreadOut] = []

    for _, tgt_id, data in analytics.bundle.directed.out_edges(account_id, data=True):
        peer_c = analytics.simclusters.primary_community.get(tgt_id)
        if peer_c != my_prim_c and tgt_id in accts_by_id:
            peer = accts_by_id[tgt_id]
            c_obj = comm_by_id.get(peer_c) if peer_c is not None else None
            cross_ties.append(
                CrossCommunityThreadOut(
                    peer_account_id=peer.id,
                    peer_handle=peer.handle,
                    peer_display_name=peer.display_name or peer.handle,
                    peer_community_name=(c_obj.name if c_obj else "Network"),
                    peer_community_color=(c_obj.color if c_obj else "#a78bfa"),
                    edge_type=str(data.get("edge_type", "follow")),
                    decayed_weight=round(float(data.get("weight", 0.0)), 2),
                    direction="outbound",
                )
            )

    for src_id, _, data in analytics.bundle.directed.in_edges(account_id, data=True):
        peer_c = analytics.simclusters.primary_community.get(src_id)
        if peer_c != my_prim_c and src_id in accts_by_id:
            peer = accts_by_id[src_id]
            c_obj = comm_by_id.get(peer_c) if peer_c is not None else None
            cross_ties.append(
                CrossCommunityThreadOut(
                    peer_account_id=peer.id,
                    peer_handle=peer.handle,
                    peer_display_name=peer.display_name or peer.handle,
                    peer_community_name=(c_obj.name if c_obj else "Network"),
                    peer_community_color=(c_obj.color if c_obj else "#a78bfa"),
                    edge_type=str(data.get("edge_type", "follow")),
                    decayed_weight=round(float(data.get("weight", 0.0)), 2),
                    direction="inbound",
                )
            )

    cross_ties.sort(key=lambda t: -t.decayed_weight)

    rel_info = analytics.relationships.by_account.get(account_id)
    mutual_history: dict[str, Any] = {
        "has_own_handle": analytics.relationships.has_own_handle,
        "my_x_handle": analytics.relationships.my_x_handle,
        "relationship_state": acct_out.relationship_state,
        "is_mutual_20x": acct_out.is_mutual_20x,
        "follows_me": acct_out.follows_me,
        "followed_by_me": acct_out.followed_by_me,
        "mutual_interaction_count_30d": acct_out.mutual_interaction_count_30d,
        "last_interaction_at": acct_out.last_interaction_at,
        "roi_score": rel_info.roi_score if rel_info else 0.0,
        "recommended_play": rel_info.recommended_play if rel_info else "",
    }

    return AccountDetailOut(
        account=acct_out,
        catalyzed_conversations=catalyzed_list[:10],
        recent_posts=recent_posts_out,
        cross_community_threads=cross_ties[:10],
        mutual_history=mutual_history,
    )


@router.get("/relationships", response_model=RelationshipsOverviewOut)
async def get_network_relationships(
    workspace_id: int,
    state: str | None = Query(default=None),
    community_id: int | None = Query(default=None),
    session: AsyncSession = Depends(get_db_session),
) -> RelationshipsOverviewOut:
    _, comms, accts, mem_map, _, _, _, analytics = await _load_full_context(
        session, workspace_id
    )
    rel_res = analytics.relationships

    serialized_by_id = {
        a.id: _serialize_network_account(a, comms, mem_map, analytics)
        for a in accts
    }

    roi_items: list[RelationshipRoiItemOut] = []
    for item in rel_res.roi_queue:
        if community_id is not None and not _account_matches_community(
            item.account_id, community_id, mem_map, analytics
        ):
            continue
        if item.account_id not in serialized_by_id:
            continue
        roi_items.append(
            RelationshipRoiItemOut(
                account=serialized_by_id[item.account_id],
                roi_score=item.roi_score,
                reciprocation_readiness=item.reciprocation_readiness,
                recommended_play=item.recommended_play,
                unlocks_20x_on_follow_back=item.unlocks_20x_on_follow_back,
                x_profile_url=f"https://x.com/{item.handle}",
            )
        )

    filtered_accounts: list[NetworkAccountOut] = []
    state_filter = (state or "").strip().lower()
    for a in accts:
        if community_id is not None and not _account_matches_community(
            a.id, community_id, mem_map, analytics
        ):
            continue
        ser = serialized_by_id[a.id]
        if state_filter and state_filter != "all" and ser.relationship_state != state_filter:
            continue
        filtered_accounts.append(ser)

    filtered_accounts.sort(
        key=lambda x: (-x.heavy_hitter_score, -x.bridge_score, x.id)
    )

    return RelationshipsOverviewOut(
        workspace_id=workspace_id,
        has_own_handle=rel_res.has_own_handle,
        my_x_handle=rel_res.my_x_handle,
        bidirectional_reply_weight=20.0,
        counts=rel_res.counts,
        roi_queue=roi_items,
        accounts=filtered_accounts,
    )
