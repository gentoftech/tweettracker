"""Story 1.4: 5-State Mutual vs. One-Way CRM & Prioritized Relationship ROI Queue."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from tweettracker.graph.builder import AccountNodeInput, PostInteractionInput

VALID_RELATIONSHIP_STATES = (
    "mutual_active",
    "mutual_dormant",
    "outbound_warming",
    "outbound_cold",
    "inbound_unreciprocated",
    "none",
)

ACTIONABLE_ROI_STATES = (
    "inbound_unreciprocated",
    "outbound_warming",
    "mutual_dormant",
)


def _ensure_utc(dt: datetime | None, fallback: datetime) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass(slots=True)
class AccountRelationshipMetrics:
    account_id: int
    handle: str
    relationship_state: str
    is_mutual_20x: bool
    follows_me: bool
    followed_by_me: bool
    mutual_interaction_count_30d: int
    last_interaction_at: datetime | None
    reciprocation_readiness: float
    roi_score: float
    recommended_play: str
    unlocks_20x_on_follow_back: bool


@dataclass(slots=True)
class RelationshipsResult:
    has_own_handle: bool
    my_x_handle: str | None
    counts: dict[str, int]
    by_account: dict[int, AccountRelationshipMetrics]
    roi_queue: list[AccountRelationshipMetrics]


def compute_relationships_and_roi(
    my_x_handle: str | None,
    accounts: Sequence[AccountNodeInput],
    heavy_hitter_scores: dict[int, float],
    bridge_scores: dict[int, float],
    post_interactions: Sequence[PostInteractionInput] = (),
    *,
    reference_time: datetime | None = None,
) -> RelationshipsResult:
    """Classifies accounts into 5 mutual/one-way states & builds Relationship ROI Queue."""
    now = reference_time or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    cleaned_my = (my_x_handle or "").strip().lstrip("@").strip()
    counts: dict[str, int] = {state: 0 for state in VALID_RELATIONSHIP_STATES}

    if not cleaned_my:
        by_acct_none: dict[int, AccountRelationshipMetrics] = {}
        for acct in accounts:
            counts["none"] += 1
            by_acct_none[acct.id] = AccountRelationshipMetrics(
                account_id=acct.id,
                handle=acct.handle,
                relationship_state="none",
                is_mutual_20x=False,
                follows_me=acct.follows_me,
                followed_by_me=acct.followed_by_me,
                mutual_interaction_count_30d=0,
                last_interaction_at=_ensure_utc(acct.last_interaction_at, now),
                reciprocation_readiness=0.0,
                roi_score=0.0,
                recommended_play="Configure My X Handle in Settings to enable 20x Mutual CRM.",
                unlocks_20x_on_follow_back=False,
            )
        return RelationshipsResult(
            has_own_handle=False,
            my_x_handle=None,
            counts=counts,
            by_account=by_acct_none,
            roi_queue=[],
        )

    my_lower = cleaned_my.lower()
    cutoff_30d = now - timedelta(days=30)
    cutoff_14d = now - timedelta(days=14)

    my_acct_ids = {a.id for a in accounts if a.handle.lower() == my_lower}
    obs_count_30d: dict[int, int] = {a.id: 0 for a in accounts}
    obs_latest_at: dict[int, datetime | None] = {a.id: None for a in accounts}

    for pi in post_interactions:
        occ = _ensure_utc(pi.occurred_at, now) or now
        if occ < cutoff_30d:
            continue
        actor_h = (pi.actor_handle or "").strip().lstrip("@").lower()
        # Case 1: my_x_handle engaged on tracked author's post
        if actor_h == my_lower and pi.author_account_id in obs_count_30d:
            tgt = pi.author_account_id
            obs_count_30d[tgt] += 1
            prev = obs_latest_at[tgt]
            if prev is None or occ > prev:
                obs_latest_at[tgt] = occ
        # Case 2: tracked actor engaged on my_x_handle's post
        elif (
            pi.author_account_id in my_acct_ids
            and pi.actor_account_id is not None
            and pi.actor_account_id in obs_count_30d
        ):
            src = pi.actor_account_id
            obs_count_30d[src] += 1
            prev = obs_latest_at[src]
            if prev is None or occ > prev:
                obs_latest_at[src] = occ

    by_account: dict[int, AccountRelationshipMetrics] = {}
    roi_candidates: list[AccountRelationshipMetrics] = []

    for acct in accounts:
        last_dt = _ensure_utc(acct.last_interaction_at, now)
        obs_dt = obs_latest_at.get(acct.id)
        if obs_dt is not None and (last_dt is None or obs_dt > last_dt):
            last_dt = obs_dt

        eff_cnt = max(int(acct.mutual_interaction_count_30d or 0), obs_count_30d.get(acct.id, 0))
        is_recent_active = (eff_cnt > 0) or (last_dt is not None and last_dt >= cutoff_14d)

        follows_me = bool(acct.follows_me)
        followed_by_me = bool(acct.followed_by_me)

        if follows_me and followed_by_me:
            state = "mutual_active" if is_recent_active else "mutual_dormant"
            is_mut_20x = True
        elif follows_me and not followed_by_me:
            state = "inbound_unreciprocated"
            is_mut_20x = False
        else:
            state = "outbound_warming" if is_recent_active else "outbound_cold"
            is_mut_20x = False

        counts[state] = counts.get(state, 0) + 1

        hh = max(0.0, float(heavy_hitter_scores.get(acct.id, 0.0)))
        bs = max(0.0, float(bridge_scores.get(acct.id, 0.0)))

        if state == "inbound_unreciprocated":
            readiness = 1.40
            play = "Follow back on X -> instantly unlocks 20x Mutual Reply Boost (+15.0)"
            unlocks_20x = True
        elif state == "outbound_warming":
            readiness = round(1.00 + 0.05 * min(eff_cnt, 6), 2)
            play = "Warm via OON-safe Quote-Post + thoughtful thread reply to earn follow-back"
            unlocks_20x = False
        elif state == "mutual_dormant":
            readiness = 0.85
            play = "Re-activate dormant 20x mutual via early Reply on their next original post"
            unlocks_20x = False
        elif state == "mutual_active":
            readiness = 1.00
            play = "20x Mutual Boost live — prioritize Hour-1 Replies on original/quote posts"
            unlocks_20x = False
        else:
            readiness = 0.35
            play = "Cold 1-way — lead with high-signal Quote-Posts (avoid cold replies dropped by OON filter)"
            unlocks_20x = False

        # ROI Formula: H_u * (1 + 0.5 * B_u / 100) * ReciprocationReadiness
        roi = round(hh * (1.0 + 0.5 * (bs / 100.0)) * readiness, 2)

        metrics = AccountRelationshipMetrics(
            account_id=acct.id,
            handle=acct.handle,
            relationship_state=state,
            is_mutual_20x=is_mut_20x,
            follows_me=follows_me,
            followed_by_me=followed_by_me,
            mutual_interaction_count_30d=eff_cnt,
            last_interaction_at=last_dt,
            reciprocation_readiness=readiness,
            roi_score=roi,
            recommended_play=play,
            unlocks_20x_on_follow_back=unlocks_20x,
        )
        by_account[acct.id] = metrics
        if state in ACTIONABLE_ROI_STATES:
            roi_candidates.append(metrics)

    roi_candidates.sort(
        key=lambda m: (
            -m.roi_score,
            0 if m.relationship_state == "inbound_unreciprocated" else 1,
            m.account_id,
        )
    )

    return RelationshipsResult(
        has_own_handle=True,
        my_x_handle=cleaned_my,
        counts=counts,
        by_account=by_account,
        roi_queue=roi_candidates,
    )
