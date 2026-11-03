"""14-day exponentially time-decayed behavioral + follow graph builder (G = (V, E, w))."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
import math

import networkx as nx

HALF_LIFE_DAYS: float = 14.0

# Grounded in xai-value-model (9106e9f):
# Reply base = 5.0, BidirectionalFollowReplyWeightBoost = +15.0 -> 20.0 mutual reply weight
MUTUAL_REPLY_WEIGHT: float = 20.0
REPLY_WEIGHT: float = 5.0
QUOTE_WEIGHT: float = 5.0
REPOST_WEIGHT: float = 0.75
LIKE_WEIGHT: float = 0.66
FOLLOW_WEIGHT: float = 2.0
MUTUAL_FOLLOW_BONUS: float = 4.0

_EDGE_PRIORITY = {
    "mutual_reply": 5,
    "quote": 4,
    "reply": 3,
    "repost": 2,
    "follow": 1,
}


def _ensure_utc(dt: datetime | None, fallback: datetime) -> datetime:
    if dt is None:
        return fallback
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def decay_multiplier(
    event_time: datetime | None,
    reference_time: datetime,
    *,
    half_life_days: float = HALF_LIFE_DAYS,
) -> float:
    """Computes 2^(-Delta_t / 14d) exponential behavioral decay."""
    if event_time is None:
        return 1.0
    ev = _ensure_utc(event_time, reference_time)
    delta_secs = max(0.0, (reference_time - ev).total_seconds())
    delta_days = delta_secs / 86_400.0
    return math.pow(2.0, -delta_days / max(half_life_days, 1e-6))


@dataclass(slots=True)
class AccountNodeInput:
    id: int
    handle: str
    display_name: str = ""
    followers_count: int = 0
    following_count: int = 0
    follows_me: bool = False
    followed_by_me: bool = False
    is_auto_tracked: bool = False
    auto_tracked_at: datetime | None = None
    created_at: datetime | None = None
    last_interaction_at: datetime | None = None
    mutual_interaction_count_30d: int = 0
    yield_score: float | None = None
    reply_sample_count: int = 0
    avg_reply_views: float = 0.0
    attributed_follows_30d: float = 0.0
    has_recent_climbing_post: bool = False
    baseline_p50: dict[int, float] = field(default_factory=dict)


@dataclass(slots=True)
class RelationshipEdgeInput:
    source_account_id: int
    target_account_id: int
    edge_type: str
    interaction_count: int = 1
    last_event_at: datetime | None = None


@dataclass(slots=True)
class PostInteractionInput:
    post_id: int
    author_account_id: int
    actor_account_id: int | None
    actor_x_user_id: str
    actor_handle: str
    interaction_type: str
    occurred_at: datetime | None = None


@dataclass(slots=True)
class BehavioralGraphBundle:
    directed: nx.DiGraph
    undirected: nx.Graph
    node_ids: list[int]
    follow_pairs: set[tuple[int, int]]
    mutual_follow_pairs: set[tuple[int, int]]
    external_engager_edges: list[tuple[str, int, float]]


def _interaction_base_weight(interaction_type: str, is_mutual_follow: bool) -> float:
    kind = interaction_type.strip().lower()
    if kind == "reply":
        return MUTUAL_REPLY_WEIGHT if is_mutual_follow else REPLY_WEIGHT
    if kind == "quote":
        return QUOTE_WEIGHT
    if kind in ("repost", "retweet"):
        return REPOST_WEIGHT
    if kind in ("like", "favorite"):
        return LIKE_WEIGHT
    return 1.0


def build_behavioral_graph(
    accounts: Sequence[AccountNodeInput],
    relationship_edges: Sequence[RelationshipEdgeInput],
    post_interactions: Sequence[PostInteractionInput] = (),
    *,
    reference_time: datetime | None = None,
    half_life_days: float = HALF_LIFE_DAYS,
) -> BehavioralGraphBundle:
    """Constructs directed G=(V, E, w) and symmetrized undirected graph in <25ms for 5k nodes."""
    now = _ensure_utc(reference_time, datetime.now(timezone.utc))
    node_ids = [a.id for a in accounts]
    valid_nodes = set(node_ids)

    directed = nx.DiGraph()
    for acct in accounts:
        directed.add_node(
            acct.id,
            handle=acct.handle,
            display_name=acct.display_name,
            followers_count=acct.followers_count,
            following_count=acct.following_count,
            follows_me=acct.follows_me,
            followed_by_me=acct.followed_by_me,
            is_auto_tracked=acct.is_auto_tracked,
            auto_tracked_at=acct.auto_tracked_at,
        )

    follow_pairs: set[tuple[int, int]] = set()
    for edge in relationship_edges:
        u = edge.source_account_id
        v = edge.target_account_id
        if u == v or u not in valid_nodes or v not in valid_nodes:
            continue
        if edge.edge_type.strip().lower() == "follow":
            follow_pairs.add((u, v))

    mutual_follow_pairs: set[tuple[int, int]] = {
        (u, v) for (u, v) in follow_pairs if (v, u) in follow_pairs
    }

    follow_w: dict[tuple[int, int], float] = {}
    inter_w: dict[tuple[int, int], float] = {}
    primary_kind: dict[tuple[int, int], str] = {}

    def _record_kind(pair: tuple[int, int], kind: str) -> None:
        cur = primary_kind.get(pair)
        if cur is None or _EDGE_PRIORITY.get(kind, 0) > _EDGE_PRIORITY.get(cur, 0):
            primary_kind[pair] = kind

    for u, v in follow_pairs:
        pair = (u, v)
        is_mut = pair in mutual_follow_pairs
        follow_w[pair] = FOLLOW_WEIGHT + (MUTUAL_FOLLOW_BONUS if is_mut else 0.0)
        _record_kind(pair, "follow")

    for edge in relationship_edges:
        u = edge.source_account_id
        v = edge.target_account_id
        if u == v or u not in valid_nodes or v not in valid_nodes:
            continue
        etype = edge.edge_type.strip().lower()
        if etype == "follow":
            continue
        pair = (u, v)
        is_mut = pair in mutual_follow_pairs
        base_w = _interaction_base_weight(etype, is_mut)
        decay = decay_multiplier(edge.last_event_at, now, half_life_days=half_life_days)
        cnt = max(1, int(edge.interaction_count))
        w_add = cnt * base_w * decay
        inter_w[pair] = inter_w.get(pair, 0.0) + w_add
        _record_kind(pair, "mutual_reply" if (etype == "reply" and is_mut) else etype)

    external_acc: dict[tuple[str, int], float] = {}
    for pi in post_interactions:
        v = pi.author_account_id
        if v not in valid_nodes:
            continue
        u = pi.actor_account_id
        etype = pi.interaction_type.strip().lower()
        decay = decay_multiplier(pi.occurred_at, now, half_life_days=half_life_days)
        if u is not None and u in valid_nodes:
            if u == v:
                continue
            pair = (u, v)
            is_mut = pair in mutual_follow_pairs
            base_w = _interaction_base_weight(etype, is_mut)
            inter_w[pair] = inter_w.get(pair, 0.0) + base_w * decay
            _record_kind(pair, "mutual_reply" if (etype == "reply" and is_mut) else etype)
        else:
            ext_key = (pi.actor_x_user_id or pi.actor_handle or "").strip().lower()
            if ext_key:
                base_w = _interaction_base_weight(etype, False)
                k = (ext_key, v)
                external_acc[k] = external_acc.get(k, 0.0) + base_w * decay

    all_pairs = set(follow_w.keys()) | set(inter_w.keys())
    undir_weights: dict[tuple[int, int], float] = {}

    edge_tuples: list[tuple[int, int, dict[str, object]]] = []
    for u, v in all_pairs:
        fw = follow_w.get((u, v), 0.0)
        iw = inter_w.get((u, v), 0.0)
        tot = fw + iw
        if tot <= 0.0:
            continue
        is_mut = (u, v) in mutual_follow_pairs
        kind = primary_kind.get((u, v), "follow")
        edge_tuples.append(
            (
                u,
                v,
                {
                    "weight": tot,
                    "follow_weight": fw,
                    "interaction_weight": iw,
                    "is_mutual_follow": is_mut,
                    "edge_type": kind,
                },
            )
        )
        sym_pair = (u, v) if u < v else (v, u)
        undir_weights[sym_pair] = undir_weights.get(sym_pair, 0.0) + tot

    directed.add_edges_from(edge_tuples)

    undirected = nx.Graph()
    undirected.add_nodes_from(node_ids)
    undirected.add_edges_from(
        (u, v, {"weight": w}) for (u, v), w in undir_weights.items() if w > 0.0
    )

    external_edges = [(ext_id, prod_id, w) for (ext_id, prod_id), w in external_acc.items() if w > 0.0]

    return BehavioralGraphBundle(
        directed=directed,
        undirected=undirected,
        node_ids=node_ids,
        follow_pairs=follow_pairs,
        mutual_follow_pairs=mutual_follow_pairs,
        external_engager_edges=external_edges,
    )
