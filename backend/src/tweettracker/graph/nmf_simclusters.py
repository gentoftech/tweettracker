"""Story 1.1: K-Component Behavioral Mini-SimClusters (Seeded NMF) + Louvain/SLPA sub-pockets."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
import math
from typing import Any
import warnings

import networkx as nx
import numpy as np
import scipy.sparse as sp
from sklearn.decomposition import NMF
from sklearn.exceptions import ConvergenceWarning

from tweettracker.algo.slpa import slpa_overlapping_communities
from tweettracker.algo.user_cred import UserCredV2
from tweettracker.graph.builder import AccountNodeInput, BehavioralGraphBundle
from tweettracker.graph.reply_yield import SEED_BOOTSTRAP_GRACE_HOURS

AUTO_CLUSTER_PALETTE: tuple[str, ...] = (
    "#38bdf8",  # Sky
    "#a78bfa",  # Violet
    "#34d399",  # Emerald
    "#fbbf24",  # Amber
    "#fb7185",  # Rose
    "#22d3ee",  # Cyan
    "#818cf8",  # Indigo
    "#a3e635",  # Lime
)


@dataclass(slots=True)
class CommunityAnchorInput:
    id: int
    name: str
    slug: str = ""
    color: str = "#38bdf8"


@dataclass(slots=True)
class SimClustersResult:
    known_for: dict[int, dict[int, float]]
    interested_in: dict[int, dict[int, float]]
    primary_community: dict[int, int | None]
    sub_pocket_labels: dict[int, str]
    slpa_labels: dict[int, list[int]]
    auto_cluster_ids: dict[int, int] = field(default_factory=dict)
    auto_cluster_colors: dict[int, str] = field(default_factory=dict)
    core_affinity: dict[int, float] = field(default_factory=dict)
    yield_scores: dict[int, float] = field(default_factory=dict)
    core_anchor_ids: set[int] = field(default_factory=set)
    engager_producer_matrix: Any = None


@dataclass(slots=True)
class CoreGraphContext:
    """Precomputed SimClusters core anchor state for scoring both tracked nodes & untracked candidates."""

    core_ids: set[int]
    core_weights: dict[int, float]
    core_omega: dict[int, float]
    mean_core_weight: float
    pockets: list[tuple[list[int], float]]
    pocket_idx_by_node: dict[int, int]
    pocket_denoms: tuple[float, ...]
    all_core_denom: float
    kappa_follow: float
    pr_by_node: dict[int, float]
    out_deg_by_node: dict[int, float]
    cred_by_node: dict[int, float]
    total_nodes: int
    has_own_handle: bool


def _colors_from_parts(
    g: nx.Graph,
    node_ids: Sequence[int],
    raw_parts: Sequence[set[int]],
) -> tuple[dict[int, int], dict[int, str]]:
    """Assigns deterministic palette colors from Louvain partitions (isolated degree-0 seeds -> #38bdf8)."""
    if not node_ids:
        return {}, {}
    if len(node_ids) < 2 or g.number_of_edges() == 0:
        return (
            {u: -1 for u in node_ids},
            {u: AUTO_CLUSTER_PALETTE[0] for u in node_ids},
        )

    connected_parts: list[set[int]] = []
    isolated_nodes: list[int] = []
    for part in raw_parts:
        if len(part) == 1:
            only_u = next(iter(part))
            if g.degree(only_u) == 0:
                isolated_nodes.append(only_u)
                continue
        if part:
            connected_parts.append(part)

    ordered_parts = sorted(
        connected_parts, key=lambda s: (-len(s), min(s) if s else 0)
    )

    cluster_ids: dict[int, int] = {}
    cluster_colors: dict[int, str] = {}
    for c_idx, part in enumerate(ordered_parts):
        color = AUTO_CLUSTER_PALETTE[c_idx % len(AUTO_CLUSTER_PALETTE)]
        for u in part:
            cluster_ids[u] = c_idx
            cluster_colors[u] = color

    for u in isolated_nodes:
        cluster_ids[u] = -1
        cluster_colors[u] = AUTO_CLUSTER_PALETTE[0]

    for u in node_ids:
        if u not in cluster_colors:
            cluster_ids[u] = -1
            cluster_colors[u] = AUTO_CLUSTER_PALETTE[0]

    return cluster_ids, cluster_colors


def _assign_sub_pockets(
    bundle: BehavioralGraphBundle,
    communities: Sequence[CommunityAnchorInput],
    interested_in: dict[int, dict[int, float]],
    manual_tags: dict[int, set[int]],
) -> tuple[
    dict[int, int | None],
    dict[int, str],
    dict[int, list[int]],
    dict[int, int],
    dict[int, str],
]:
    node_ids = bundle.node_ids
    if not node_ids:
        return {}, {}, {}, {}, {}

    n_nodes = len(node_ids)
    slpa_iters = 5 if n_nodes > 800 else (10 if n_nodes > 300 else 18)
    slpa_map = slpa_overlapping_communities(
        bundle.undirected,
        iterations=slpa_iters,
        threshold=0.25,
        weight="weight",
        seed=42,
    )

    if not communities:
        g = bundle.undirected
        if n_nodes >= 2 and g.number_of_edges() > 0:
            louvain_tol = 1e-3 if n_nodes > 400 else 1e-6
            parts = list(
                nx.community.louvain_communities(
                    g, weight="weight", resolution=1.0, threshold=louvain_tol, seed=42
                )
            )
        else:
            parts = [{u} for u in node_ids]
        auto_ids, auto_cols = _colors_from_parts(g, node_ids, parts)
        return (
            {u: None for u in node_ids},
            {u: "Ecosystem Core #1" for u in node_ids},
            slpa_map,
            auto_ids,
            auto_cols,
        )

    comm_by_id = {c.id: c for c in communities}
    primary_comm: dict[int, int | None] = {}
    buckets: dict[int, list[int]] = {c.id: [] for c in communities}

    for u in node_ids:
        aff = interested_in.get(u, {})
        if aff:
            best_cid = max(
                communities,
                key=lambda c: (
                    aff.get(c.id, 0.0),
                    1 if c.id in manual_tags.get(u, set()) else 0,
                    -c.id,
                ),
            ).id
        else:
            best_cid = communities[0].id
        primary_comm[u] = best_cid
        buckets[best_cid].append(u)

    sub_pocket_labels: dict[int, str] = {}
    collected_parts: list[set[int]] = []
    for comm in communities:
        members = buckets[comm.id]
        if not members:
            continue
        sub_g = bundle.undirected.subgraph(members)
        if len(members) >= 2 and sub_g.number_of_edges() > 0:
            louvain_tol = 1e-3 if len(members) > 400 else 1e-6
            raw_parts = list(
                nx.community.louvain_communities(
                    sub_g,
                    weight="weight",
                    resolution=1.0,
                    threshold=louvain_tol,
                    seed=42,
                )
            )
            ordered_parts = sorted(
                raw_parts, key=lambda s: (-len(s), min(s) if s else 0)
            )
        else:
            ordered_parts = [{u} for u in members]

        collected_parts.extend(ordered_parts)
        for p_idx, part in enumerate(ordered_parts, start=1):
            for u in part:
                is_slpa_multi = len(slpa_map.get(u, [])) >= 2
                kind = "Bridge Pocket" if is_slpa_multi and len(communities) >= 2 else "Core"
                sub_pocket_labels[u] = f"{comm_by_id[comm.id].name} {kind} #{p_idx}"

    auto_ids, auto_cols = _colors_from_parts(bundle.undirected, node_ids, collected_parts)
    return primary_comm, sub_lbls_or_empty(sub_pocket_labels), slpa_map, auto_ids, auto_cols


def sub_lbls_or_empty(d: dict[int, str]) -> dict[int, str]:
    return d


def _ensure_utc(dt: datetime | None, fallback: datetime) -> datetime:
    if dt is None:
        return fallback
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _build_engager_producer_matrices(
    bundle: BehavioralGraphBundle,
    node_idx: dict[int, int],
) -> tuple[sp.csr_matrix, sp.csr_matrix]:
    """Builds sparse tracked (P x P) and full bipartite ((P + E) x P) log-behavioral matrices."""
    p_count = len(bundle.node_ids)
    rows: list[int] = []
    cols: list[int] = []
    vals: list[float] = []

    for u, v, data in bundle.directed.edges(data=True):
        if u not in node_idx or v not in node_idx:
            continue
        i = node_idx[u]
        j = node_idx[v]
        w = float(data.get("weight", 1.0))
        if w > 0.0:
            rows.append(i)
            cols.append(j)
            vals.append(math.log1p(w))

    m_tracked = sp.csr_matrix(
        (
            np.array(vals, dtype=np.float64),
            (np.array(rows, dtype=np.int32), np.array(cols, dtype=np.int32)),
        ),
        shape=(p_count, p_count),
    )

    ext_keys: dict[str, int] = {}
    ext_rows: list[int] = []
    ext_cols: list[int] = []
    ext_vals: list[float] = []
    for ext_uid, prod_u, w in bundle.external_engager_edges:
        if prod_u not in node_idx or w <= 0.0:
            continue
        r_idx = ext_keys.get(ext_uid)
        if r_idx is None:
            r_idx = len(ext_keys)
            ext_keys[ext_uid] = r_idx
        ext_rows.append(r_idx)
        ext_cols.append(node_idx[prod_u])
        ext_vals.append(math.log1p(w))

    if ext_keys:
        m_ext = sp.csr_matrix(
            (
                np.array(ext_vals, dtype=np.float64),
                (np.array(ext_rows, dtype=np.int32), np.array(ext_cols, dtype=np.int32)),
            ),
            shape=(len(ext_keys), p_count),
        )
        m_engager_prod = sp.vstack([m_tracked, m_ext], format="csr")
    else:
        m_engager_prod = m_tracked
    return m_tracked, m_engager_prod


GLOBAL_MASS_EQUIV_NODES: float = 2_500_000.0
BOOTSTRAP_GRACE_HOURS: float = 48.0
BOOTSTRAP_DECAY_TAU_HOURS: float = 72.0
REGULARIZED_COSINE_PRIOR_SQ: float = 2.25  # 1.5^2 shrinkage prior on SimClusters vector norm
# SimClusters producer similarity is a cosine over full follower sets, so a huge audience dilutes
# how "close" a passive co-follow makes an account. We only see the core's side of that overlap,
# so follow-only ties are discounted by audience size above this pivot (replies/quotes are not).
FOLLOW_SPECIFICITY_PIVOT_FOLLOWERS: int = 50_000


def follow_specificity(followers_count: int | None) -> float:
    """1.0 up to 50k followers, then 1 / (1 + log10(followers / 50k)): 500k -> 0.5, 5M -> 0.33."""
    foll = int(followers_count or 0)
    if foll <= FOLLOW_SPECIFICITY_PIVOT_FOLLOWERS:
        return 1.0
    return 1.0 / (1.0 + math.log10(foll / float(FOLLOW_SPECIFICITY_PIVOT_FOLLOWERS)))


def build_core_graph_context(
    accounts: Sequence[AccountNodeInput],
    *,
    my_x_handle: str | None = None,
    auto_cluster_ids: dict[int, int] | None = None,
    primary_community: dict[int, int | None] | None = None,
    pr_by_node: dict[int, float] | None = None,
    out_deg_by_node: dict[int, float] | None = None,
    cred_by_node: dict[int, float] | None = None,
    reference_time: datetime | None = None,
) -> CoreGraphContext:
    """Constructs the Yield-Learned Core Anchor context C_yield (cold-starting from bootstrap seeds + 20x mutuals)."""
    has_own_handle = bool(my_x_handle and my_x_handle.strip())
    n_total = len(accounts)
    if n_total == 0:
        return CoreGraphContext(
            core_ids=set(),
            core_weights={},
            core_omega={},
            mean_core_weight=1.0,
            pockets=[],
            pocket_idx_by_node={},
            pocket_denoms=(),
            all_core_denom=2.0,
            kappa_follow=2.2,
            pr_by_node={},
            out_deg_by_node={},
            cred_by_node={},
            total_nodes=0,
            has_own_handle=has_own_handle,
        )

    pr_map = dict(pr_by_node or {})
    out_map = dict(out_deg_by_node or {})
    cred_map = dict(cred_by_node or {})
    uniform_pr = 1.0 / float(n_total)
    for acct in accounts:
        pr_map.setdefault(acct.id, uniform_pr)
        out_map.setdefault(acct.id, 0.0)
        cred_map.setdefault(acct.id, 50.0)

    ref_utc = _ensure_utc(reference_time, datetime.now(timezone.utc)) if reference_time is not None else None

    def _seed_past_bootstrap_grace(acct: AccountNodeInput) -> bool:
        if acct.created_at is None:
            return False
        eff_ref = ref_utc if ref_utc is not None else datetime.now(timezone.utc)
        age_hours = max(
            0.0,
            (eff_ref - _ensure_utc(acct.created_at, eff_ref)).total_seconds() / 3600.0,
        )
        return age_hours > SEED_BOOTSTRAP_GRACE_HOURS

    proven_high_yield: set[int] = set()
    faded_ids: set[int] = set()
    for acct in accounts:
        y = acct.yield_score
        n_rep = int(acct.reply_sample_count or 0)
        if n_rep >= 1 and y is not None and y >= 0.55:
            proven_high_yield.add(acct.id)
        elif y is not None and (
            (n_rep >= 2 and y < 0.42)
            or (n_rep == 1 and y < 0.32)
            or (
                not acct.is_auto_tracked
                and n_rep == 0
                and y < 0.40
                and _seed_past_bootstrap_grace(acct)
            )
        ):
            faded_ids.add(acct.id)

    core_ids: set[int] = set(proven_high_yield)
    for acct in accounts:
        if acct.id in faded_ids:
            continue
        is_mutual = has_own_handle and bool(acct.follows_me and acct.followed_by_me)
        if is_mutual:
            core_ids.add(acct.id)
        elif not acct.is_auto_tracked:
            if len(proven_high_yield) < 5 or acct.yield_score is None or acct.yield_score >= 0.48:
                core_ids.add(acct.id)

    if not core_ids:
        ranked_fallback = sorted(
            accounts,
            key=lambda a: (
                -(a.yield_score if a.yield_score is not None else 0.5),
                -(1 if (has_own_handle and a.follows_me and a.followed_by_me) else 0),
                -int(a.followers_count or 0),
                a.id,
            ),
        )
        core_ids = {a.id for a in ranked_fallback[: min(len(ranked_fallback), 10)]}

    core_weights: dict[int, float] = {}
    acct_by_id = {a.id: a for a in accounts}
    for cid in core_ids:
        acct = acct_by_id[cid]
        is_mutual = has_own_handle and bool(acct.follows_me and acct.followed_by_me)
        y = acct.yield_score
        n_rep = int(acct.reply_sample_count or 0)
        if n_rep >= 1 and y is not None:
            base_anchor = 0.65 + 0.85 * min(max(float(y), 0.0), 1.0)
        else:
            base_anchor = 1.0 if not acct.is_auto_tracked else 0.80
        mutual_boost = 1.30 if is_mutual else 1.0
        cred_val = float(cred_map.get(cid, 50.0))
        cred_mod = 0.65 + 0.35 * min(max(cred_val / 100.0, 0.0), 1.0)
        core_weights[cid] = base_anchor * mutual_boost * cred_mod

    mean_w = sum(core_weights.values()) / float(max(len(core_weights), 1))
    mean_w = max(mean_w, 1e-6)
    core_omega = {cid: w / mean_w for cid, w in core_weights.items()}

    all_sq = sum(w * w for w in core_weights.values())
    pad_all = max(0, 4 - len(core_ids)) * (mean_w * mean_w)
    all_core_denom = math.sqrt(max(all_sq + pad_all, 1e-9))

    # Partition core into sub-pockets (Louvain auto-clusters or primary communities, min pocket size 3)
    pocket_buckets: dict[int, list[int]] = defaultdict(list)
    cluster_src = auto_cluster_ids or {}
    comm_src = primary_community or {}
    for cid in sorted(core_ids):
        p_key = cluster_src.get(cid, -1)
        if p_key < 0 and comm_src.get(cid) is not None:
            p_key = int(comm_src[cid] or 0) + 10_000
        if p_key >= 0:
            pocket_buckets[p_key].append(cid)

    pockets: list[tuple[list[int], float]] = []
    for p_nodes in pocket_buckets.values():
        if len(p_nodes) >= 3:
            p_sq = sum(core_weights[c] ** 2 for c in p_nodes)
            p_pad = max(0, 4 - len(p_nodes)) * (mean_w * mean_w)
            pockets.append((p_nodes, math.sqrt(max(p_sq + p_pad, 1e-9))))
    if not pockets:
        pockets.append((sorted(core_ids), all_core_denom))

    pocket_idx_by_node: dict[int, int] = {}
    pocket_denoms: tuple[float, ...] = tuple(p_denom for _, p_denom in pockets)
    for p_idx, (p_nodes, _) in enumerate(pockets):
        for cid in p_nodes:
            pocket_idx_by_node[cid] = p_idx

    kappa_follow = max(2.2, min(float(len(core_ids)) / 4.0, 3.0))

    return CoreGraphContext(
        core_ids=core_ids,
        core_weights=core_weights,
        core_omega=core_omega,
        mean_core_weight=mean_w,
        pockets=pockets,
        pocket_idx_by_node=pocket_idx_by_node,
        pocket_denoms=pocket_denoms,
        all_core_denom=all_core_denom,
        kappa_follow=kappa_follow,
        pr_by_node=pr_map,
        out_deg_by_node=out_map,
        cred_by_node=cred_map,
        total_nodes=n_total,
        has_own_handle=has_own_handle,
    )


def score_behavioral_core_affinity(
    ctx: CoreGraphContext,
    *,
    node_id: int | None,
    inbound_conv: dict[int, float],
    outbound_conv: dict[int, float],
    inbound_follow: dict[int, float],
    outbound_follow: dict[int, float],
    aud_cos_bonus: float = 0.0,
    known_cred_v2: float | None = None,
    is_auto_tracked: bool = True,
    auto_tracked_at: datetime | None = None,
    is_self_core_anchor: bool = False,
    reference_time: datetime | None = None,
    followers_count: int | None = None,
) -> float:
    """Continuous SimClusters core affinity + UserCredV2 PageRank score in [0.0, 1.0].

    Shared kernel for BOTH tracked accounts and untracked candidates (zero follower-count cliffs).
    `auto_tracked_at` anchors the co-follow bootstrap decay: callers must pass when the co-follow
    evidence first appeared (candidates) or when the account was auto-added (tracked), so the same
    account scores the same on both sides of the add/prune thresholds.
    """
    if not ctx.core_ids:
        return 0.0
    # Core anchors (manual seeds, reciprocated mutuals) define the core rather than compete to
    # join it, so the audience-size discount only applies to accounts being judged for membership.
    f_spec = 1.0 if is_self_core_anchor else follow_specificity(followers_count)

    # Sparse O(deg(u)) neighbor core keys (avoids O(|C_0|) scans on large 5,000-node graphs)
    neighbor_core_ids: set[int] = set()
    for d in (inbound_conv, outbound_conv, inbound_follow, outbound_follow):
        for k in d:
            if k != node_id and k in ctx.core_ids:
                neighbor_core_ids.add(k)

    # 1. Conversational core mass (14d-decayed replies/quotes/mutual-replies dominate)
    m_conv = 0.0
    for cid in neighbor_core_ids:
        w_conv = float(inbound_conv.get(cid, 0.0)) + 0.60 * float(outbound_conv.get(cid, 0.0))
        if w_conv > 0.0:
            # Per-endorser log dampening prevents 1 friend spamming 20 replies from faking breadth
            m_conv += ctx.core_omega[cid] * 8.0 * math.log1p(w_conv / 8.0)

    # 2. Cold-start co-follow bootstrap decay after 48h grace window if zero conversational core ties
    gamma_boot = 1.0
    if (
        is_auto_tracked
        and auto_tracked_at is not None
        and reference_time is not None
        and m_conv < 0.50
    ):
        ref_utc = _ensure_utc(reference_time, datetime.now(timezone.utc))
        auto_utc = _ensure_utc(auto_tracked_at, ref_utc)
        age_hours = max(0.0, (ref_utc - auto_utc).total_seconds() / 3600.0)
        if age_hours > BOOTSTRAP_GRACE_HOURS:
            gamma_boot = max(
                0.03,
                math.exp(-(age_hours - BOOTSTRAP_GRACE_HOURS) / BOOTSTRAP_DECAY_TAU_HOURS),
            )

    # 3. Co-follow mass normalized by Bayesian core-size prior kappa_follow
    m_follow = 0.0
    for cid in neighbor_core_ids:
        f_w = f_spec * (
            float(inbound_follow.get(cid, 0.0)) + 0.35 * float(outbound_follow.get(cid, 0.0))
        )
        if f_w > 0.0:
            m_follow += ctx.core_omega[cid] * (f_w / ctx.kappa_follow)

    self_anchor_bonus = 2.2 if is_self_core_anchor else 0.0
    m_behav = m_conv + gamma_boot * m_follow + self_anchor_bonus
    tie_score = 1.0 - math.exp(-max(0.0, m_behav) / 4.6)

    # 4. Regularized SimClusters Cosine Similarity across core sub-pockets & full core C_0
    e_vec: dict[int, float] = {}
    if is_self_core_anchor and node_id is not None and node_id in ctx.core_ids:
        e_vec[node_id] = math.log1p(6.0)
    for cid in neighbor_core_ids:
        raw_tie = (
            float(inbound_conv.get(cid, 0.0))
            + 0.60 * float(outbound_conv.get(cid, 0.0))
            + gamma_boot
            * f_spec
            * (float(inbound_follow.get(cid, 0.0)) + 0.35 * float(outbound_follow.get(cid, 0.0)))
        )
        if raw_tie > 0.0:
            e_vec[cid] = math.log1p(raw_tie)

    if e_vec:
        sq_all = 0.0
        dot_all = 0.0
        pocket_sq: dict[int, float] = {}
        pocket_dot: dict[int, float] = {}
        for c, v in e_vec.items():
            vv = v * v
            wc = v * ctx.core_weights[c]
            sq_all += vv
            dot_all += wc
            p_idx = ctx.pocket_idx_by_node.get(c)
            if p_idx is not None:
                pocket_sq[p_idx] = pocket_sq.get(p_idx, 0.0) + vv
                pocket_dot[p_idx] = pocket_dot.get(p_idx, 0.0) + wc

        norm_all = math.sqrt(sq_all + REGULARIZED_COSINE_PRIOR_SQ)
        cos_all = dot_all / max(norm_all * ctx.all_core_denom, 1e-9)

        best_pocket_cos = 0.0
        for p_idx, p_sq in pocket_sq.items():
            norm_p = math.sqrt(p_sq + REGULARIZED_COSINE_PRIOR_SQ)
            cos_p = pocket_dot[p_idx] / max(norm_p * ctx.pocket_denoms[p_idx], 1e-9)
            if cos_p > best_pocket_cos:
                best_pocket_cos = cos_p
        core_cos = min(1.0, 0.75 * best_pocket_cos + 0.25 * cos_all)
    else:
        core_cos = 0.0

    sim_cos = min(
        1.0,
        max(core_cos, 0.65 * core_cos + 0.35 * gamma_boot * max(0.0, aud_cos_bonus)),
    )

    # 5. UserCredV2 credibility (actual PageRank mass when tracked & un-decayed; virtual 1-step PageRank otherwise)
    if known_cred_v2 is not None and gamma_boot >= 0.999:
        cred_norm = min(max(float(known_cred_v2) / 100.0, 0.0), 1.0)
    else:
        total_in_w = 0.0
        pr_transfer = 0.0
        all_endorsers = set(inbound_conv.keys()) | set(inbound_follow.keys())
        for e_id in all_endorsers:
            if e_id == node_id:
                continue
            w_ep = float(inbound_conv.get(e_id, 0.0)) + gamma_boot * float(
                inbound_follow.get(e_id, 0.0)
            )
            if w_ep <= 0.0:
                continue
            total_in_w += w_ep
            pr_e = ctx.pr_by_node.get(e_id, 1.0 / float(max(ctx.total_nodes, 1)))
            out_e = max(ctx.out_deg_by_node.get(e_id, 0.0), 12.0)
            pr_transfer += pr_e * (w_ep / (out_e + w_ep))

        if total_in_w > 0.0:
            n_eff = float(max(ctx.total_nodes + 1, 2))
            pers_p = (1.0 / n_eff) * (total_in_w / (8.0 + total_in_w))
            pr_virt = 0.85 * pr_transfer + 0.15 * pers_p
            mass = pr_virt * (n_eff / GLOBAL_MASS_EQUIV_NODES)
            uid_int = abs(int(node_id)) if node_id is not None else 1
            cred_score = UserCredV2.from_mass(uid_int, mass).score
            cred_norm = min(max(cred_score / 100.0, 0.0), 1.0)
        elif is_self_core_anchor and known_cred_v2 is not None:
            cred_norm = min(max(float(known_cred_v2) / 100.0, 0.0), 1.0)
        else:
            cred_norm = 0.0

    raw_score = 0.50 * tie_score + 0.30 * sim_cos + 0.20 * cred_norm
    return round(min(1.0, max(0.0, raw_score)), 4)


def compute_core_affinity_scores(
    *,
    accounts: Sequence[AccountNodeInput],
    bundle: BehavioralGraphBundle,
    simclusters: SimClustersResult,
    pr_by_node: dict[int, float] | None = None,
    out_deg_by_node: dict[int, float] | None = None,
    cred_by_node: dict[int, float] | None = None,
    my_x_handle: str | None = None,
    reference_time: datetime | None = None,
) -> dict[int, float]:
    """Computes continuous SimClusters core affinity in [0.0, 1.0] for every tracked node (K >= 1)."""
    node_ids = bundle.node_ids
    if not node_ids or not accounts:
        return {}

    ctx = build_core_graph_context(
        accounts,
        my_x_handle=my_x_handle,
        auto_cluster_ids=simclusters.auto_cluster_ids,
        primary_community=simclusters.primary_community,
        pr_by_node=pr_by_node,
        out_deg_by_node=out_deg_by_node,
        cred_by_node=cred_by_node,
        reference_time=reference_time,
    )
    simclusters.core_anchor_ids = set(ctx.core_ids)

    node_idx = {u: idx for idx, u in enumerate(node_ids)}
    cached_m = simclusters.engager_producer_matrix
    if cached_m is not None and cached_m.shape[1] == len(node_ids):
        m_engager_prod = cached_m
    else:
        _, m_engager_prod = _build_engager_producer_matrices(bundle, node_idx)
        simclusters.engager_producer_matrix = m_engager_prod

    # Compute SimClusters bipartite audience centroid cosine `aud_cos` from M (works for K=1 and K>=2)
    p_count = len(node_ids)
    core_vec = np.zeros(p_count, dtype=np.float64)
    for cid, w_c in ctx.core_weights.items():
        if cid in node_idx:
            core_vec[node_idx[cid]] = w_c
    core_sum = float(core_vec.sum())
    if core_sum > 1e-9:
        core_vec = core_vec / core_sum

    centroid = np.asarray(m_engager_prod @ core_vec).ravel()
    centroid_norm = float(np.linalg.norm(centroid))

    aud_cos_by_node: dict[int, float] = {}
    if centroid_norm > 1e-9 and m_engager_prod.nnz > 0:
        dots = np.asarray(m_engager_prod.T @ centroid).ravel()
        col_sq = np.asarray(m_engager_prod.power(2).sum(axis=0)).ravel()
        col_norms = np.sqrt(col_sq + REGULARIZED_COSINE_PRIOR_SQ)
        cos_vals = np.clip(dots / np.maximum(col_norms * centroid_norm, 1e-9), 0.0, 1.0)
        for u, idx in node_idx.items():
            aud_cos_by_node[u] = float(cos_vals[idx])

    acct_by_id = {a.id: a for a in accounts}
    scores: dict[int, float] = {}

    for u in node_ids:
        acct = acct_by_id.get(u)
        if acct is None:
            continue

        in_conv: dict[int, float] = {}
        in_foll: dict[int, float] = {}
        for src, _, data in bundle.directed.in_edges(u, data=True):
            iw = float(data.get("interaction_weight", 0.0))
            fw = float(data.get("follow_weight", 0.0))
            if iw > 0.0:
                in_conv[int(src)] = iw
            if fw > 0.0:
                in_foll[int(src)] = fw

        out_conv: dict[int, float] = {}
        out_foll: dict[int, float] = {}
        for _, tgt, data in bundle.directed.out_edges(u, data=True):
            if tgt not in ctx.core_ids:
                continue
            iw = float(data.get("interaction_weight", 0.0))
            fw = float(data.get("follow_weight", 0.0))
            if iw > 0.0:
                out_conv[int(tgt)] = iw
            if fw > 0.0:
                out_foll[int(tgt)] = fw

        scores[u] = score_behavioral_core_affinity(
            ctx,
            node_id=u,
            inbound_conv=in_conv,
            outbound_conv=out_conv,
            inbound_follow=in_foll,
            outbound_follow=out_foll,
            aud_cos_bonus=aud_cos_by_node.get(u, 0.0),
            known_cred_v2=ctx.cred_by_node.get(u),
            is_auto_tracked=bool(acct.is_auto_tracked),
            auto_tracked_at=acct.auto_tracked_at,
            is_self_core_anchor=(u in ctx.core_ids),
            reference_time=reference_time,
            followers_count=acct.followers_count,
        )

    return scores


def compute_mini_simclusters(
    bundle: BehavioralGraphBundle,
    communities: Sequence[CommunityAnchorInput],
    manual_tags: dict[int, set[int]],
    *,
    seed_strength: float = 4.0,
) -> SimClustersResult:
    """Computes Seeded NMF KnownFor & K-dimensional normalized InterestedIn vectors."""
    node_ids = bundle.node_ids
    p_count = len(node_ids)
    k_count = len(communities)

    if p_count == 0 or k_count == 0:
        primary_c, sub_lbls, slpa_map, auto_ids, auto_cols = _assign_sub_pockets(
            bundle, communities, {}, manual_tags
        )
        return SimClustersResult(
            known_for={u: {} for u in node_ids},
            interested_in={u: {} for u in node_ids},
            primary_community=primary_c,
            sub_pocket_labels=sub_lbls,
            slpa_labels=slpa_map,
            auto_cluster_ids=auto_ids,
            auto_cluster_colors=auto_cols,
        )

    # Graceful K = 1 single-community path: exact 1.0 affinity, zero division-by-zero
    if k_count == 1:
        c0 = communities[0].id
        known_single = {u: {c0: 1.0} for u in node_ids}
        int_single = {u: {c0: 1.0} for u in node_ids}
        primary_c, sub_lbls, slpa_map, auto_ids, auto_cols = _assign_sub_pockets(
            bundle, communities, int_single, manual_tags
        )
        return SimClustersResult(
            known_for=known_single,
            interested_in=int_single,
            primary_community=primary_c,
            sub_pocket_labels=sub_lbls,
            slpa_labels=slpa_map,
            auto_cluster_ids=auto_ids,
            auto_cluster_colors=auto_cols,
        )

    node_idx = {u: idx for idx, u in enumerate(node_ids)}
    comm_ids = [c.id for c in communities]
    m_tracked, m_engager_prod = _build_engager_producer_matrices(bundle, node_idx)

    # Build manual community anchor matrix V0 (P x K)
    v0 = np.zeros((p_count, k_count), dtype=np.float64)
    has_manual = np.zeros(p_count, dtype=bool)
    for p_i, u in enumerate(node_ids):
        tags = manual_tags.get(u, set())
        tagged_indices = [c_i for c_i, cid in enumerate(comm_ids) if cid in tags]
        if tagged_indices:
            has_manual[p_i] = True
            anchor_val = 1.0 / math.sqrt(len(tagged_indices))
            for c_i in tagged_indices:
                v0[p_i, c_i] = anchor_val

    # Append K virtual seed rows sqrt(seed_strength) * V0.T so NMF optimizes seeded objective
    sqrt_lambda = math.sqrt(max(seed_strength, 0.1))
    seed_rows = sp.csr_matrix(sqrt_lambda * v0.T)
    m_aug = sp.vstack([m_engager_prod, seed_rows], format="csr")

    n_aug_rows = m_aug.shape[0]
    proj_init = m_engager_prod @ v0
    max_proj = float(proj_init.max()) if proj_init.size > 0 else 0.0
    if max_proj > 1e-9:
        proj_init = proj_init / max_proj

    w_bottom = sqrt_lambda * np.eye(k_count, dtype=np.float64) + 0.01
    w_init = np.ascontiguousarray(
        np.vstack([proj_init + 0.02, w_bottom]), dtype=np.float64
    )
    h_init = np.ascontiguousarray(v0.T + 0.02, dtype=np.float64)
    assert w_init.shape == (n_aug_rows, k_count)
    assert h_init.shape == (k_count, p_count)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ConvergenceWarning)
        nmf = NMF(
            n_components=k_count,
            init="custom",
            solver="cd",
            beta_loss="frobenius",
            max_iter=55,
            tol=1e-3,
            alpha_W=0.0,
            alpha_H=0.01,
            l1_ratio=1.0,
            random_state=42,
        )
        nmf.fit_transform(m_aug, W=w_init, H=h_init)
        v_nmf = nmf.components_.T.copy()  # shape (P, K)

    # Normalize each community factor column to comparable [0, 1] scale
    for k_i in range(k_count):
        col = v_nmf[:, k_i]
        pos = col[col > 1e-9]
        scale = float(np.percentile(pos, 95)) if pos.size > 0 else float(col.max())
        if scale > 1e-9:
            v_nmf[:, k_i] = np.clip(col / scale, 0.0, 1.25)

    # Regularize anchored producers toward V0 so factor indices stay locked to community order
    v_known = v_nmf.copy()
    if np.any(has_manual):
        v_known[has_manual] = 0.62 * v_nmf[has_manual] + 0.38 * v0[has_manual]

    # Row-scale KnownFor to [0, 1] per producer (max anchor = 1.0 when non-zero)
    row_max = v_known.max(axis=1, keepdims=True)
    v_known_scaled = np.where(row_max > 1e-9, v_known / np.maximum(row_max, 1e-9), 0.0)
    v_known_scaled = np.clip(v_known_scaled, 0.0, 1.0)

    # InterestedIn: outbound engagement M*V + inbound audience M.T*V + producer anchor
    v_l1 = v_known / np.maximum(v_known.sum(axis=1, keepdims=True), 1e-9)
    outbound_aff = m_tracked @ v_l1
    inbound_aff = m_tracked.T @ v_l1

    # Normalize neighborhood projections before blending with producer anchor
    out_sum = outbound_aff.sum(axis=1, keepdims=True)
    in_sum = inbound_aff.sum(axis=1, keepdims=True)
    out_unit = np.where(out_sum > 1e-9, outbound_aff / np.maximum(out_sum, 1e-9), 0.0)
    in_unit = np.where(in_sum > 1e-9, inbound_aff / np.maximum(in_sum, 1e-9), 0.0)

    has_out = (out_sum.ravel() > 1e-9).astype(np.float64)[:, None]
    has_in = (in_sum.ravel() > 1e-9).astype(np.float64)[:, None]

    u_raw = 0.45 * v_l1 + 0.30 * has_out * out_unit + 0.25 * has_in * in_unit
    u_sums = u_raw.sum(axis=1, keepdims=True)

    uniform_fallback = np.full((p_count, k_count), 1.0 / k_count, dtype=np.float64)
    u_norm = np.where(u_sums > 1e-12, u_raw / np.maximum(u_sums, 1e-12), uniform_fallback)
    # Re-normalize strictly so every row sums to 1.0
    u_norm = u_norm / np.maximum(u_norm.sum(axis=1, keepdims=True), 1e-12)

    known_for_dict: dict[int, dict[int, float]] = {}
    interested_in_dict: dict[int, dict[int, float]] = {}

    for p_i, u in enumerate(node_ids):
        known_for_dict[u] = {
            cid: round(float(v_known_scaled[p_i, c_i]), 4)
            for c_i, cid in enumerate(comm_ids)
        }
        # Preserve exact sum=1.0 on rounded InterestedIn dictionary
        raw_probs = [max(0.0, float(u_norm[p_i, c_i])) for c_i in range(k_count)]
        s_p = sum(raw_probs)
        if s_p <= 1e-12:
            norm_probs = [round(1.0 / k_count, 4) for _ in range(k_count)]
        else:
            norm_probs = [round(val / s_p, 4) for val in raw_probs]
        diff = round(1.0 - sum(norm_probs), 4)
        if abs(diff) > 0:
            max_i = int(np.argmax(norm_probs))
            norm_probs[max_i] = round(max(0.0, norm_probs[max_i] + diff), 4)
        interested_in_dict[u] = {
            cid: norm_probs[c_i] for c_i, cid in enumerate(comm_ids)
        }

    primary_c, sub_lbls, slpa_map, auto_ids, auto_cols = _assign_sub_pockets(
        bundle, communities, interested_in_dict, manual_tags
    )

    return SimClustersResult(
        known_for=known_for_dict,
        interested_in=interested_in_dict,
        primary_community=primary_c,
        sub_pocket_labels=sub_lbls,
        slpa_labels=slpa_map,
        auto_cluster_ids=auto_ids,
        auto_cluster_colors=auto_cols,
        engager_producer_matrix=m_engager_prod,
    )
