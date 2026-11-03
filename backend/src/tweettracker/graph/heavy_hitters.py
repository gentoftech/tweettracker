"""Story 1.2: Real Heavy Hitters (Catalysis Lift, Weighted s-Core, SALSA, UserCredV2, Tracer Reach)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
import heapq
import math
import statistics

import networkx as nx
import numpy as np
import scipy.sparse as sp

from tweettracker.algo.user_cred import UserCredV2
from tweettracker.graph.builder import (
    AccountNodeInput,
    BehavioralGraphBundle,
    PostInteractionInput,
)

_AGE_GRID = (15, 30, 45, 60, 120, 360, 1440, 2880)
_DEFAULT_AGE_FRACS = {
    15: 0.09,
    30: 0.17,
    45: 0.25,
    60: 0.33,
    120: 0.50,
    360: 0.72,
    1440: 0.91,
    2880: 1.00,
}

# Global X active-graph divisor mapping workspace PageRank onto UserCredV2's log-mass domain
GLOBAL_MASS_EQUIV_NODES: float = 2_500_000.0


@dataclass(slots=True)
class PostTrajectoryInput:
    id: int
    x_post_id: str
    author_account_id: int
    created_at_x: datetime
    is_reply: bool = False
    is_retweet: bool = False
    is_quote: bool = False
    latest_views: int = 0
    value_model_score: float = 0.0
    snapshots: list[tuple[int, int]] = field(default_factory=list)


@dataclass(slots=True)
class AccountHeavyHitterMetrics:
    heavy_hitter_score: float
    catalysis_lift: float
    catalyzed_posts_count: int
    s_core_depth: float
    salsa_authority: float
    salsa_scout: float
    user_cred_v2: float
    own_post_reach_norm: float
    follower_efficiency: float
    community_scores: dict[int, float] = field(default_factory=dict)


@dataclass(slots=True)
class HeavyHittersResult:
    by_account: dict[int, AccountHeavyHitterMetrics]
    catalyzed_events: dict[int, list[tuple[int, float]]]
    pr_by_account: dict[int, float] = field(default_factory=dict)
    out_deg_by_account: dict[int, float] = field(default_factory=dict)


def _ensure_utc(dt: datetime | None, fallback: datetime) -> datetime:
    if dt is None:
        return fallback
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def interpolate_baseline_p50(
    baseline_p50: dict[int, float],
    followers_count: int,
    age_minutes: float,
) -> float:
    """Piecewise-linear interpolation of expected P50 impressions at `age_minutes`."""
    age_clamped = max(1.0, min(float(age_minutes), 2880.0))
    est_48h = max(250.0, float(followers_count) * 0.16)

    pts: list[tuple[float, float]] = [(0.0, 0.0)]
    for g_age in _AGE_GRID:
        val = baseline_p50.get(g_age)
        if val is None or val <= 0:
            val = est_48h * _DEFAULT_AGE_FRACS[g_age]
        pts.append((float(g_age), float(val)))

    for idx in range(len(pts) - 1):
        a0, v0 = pts[idx]
        a1, v1 = pts[idx + 1]
        if a0 <= age_clamped <= a1:
            span = max(a1 - a0, 1e-6)
            t = (age_clamped - a0) / span
            return v0 + t * (v1 - v0)
    return pts[-1][1]


def compute_weighted_s_core(undirected: nx.Graph) -> dict[int, float]:
    """O((|V| + |E|) log |V|) continuous weighted s-core peel depth."""
    nodes = list(undirected.nodes())
    if not nodes:
        return {}

    adj: dict[int, list[tuple[int, float]]] = {}
    strength: dict[int, float] = {}
    heap: list[tuple[float, int]] = []

    for u in nodes:
        nbrs: list[tuple[int, float]] = []
        s_u = 0.0
        for v, data in undirected[u].items():
            w_raw = float(data.get("weight", 0.0))
            if w_raw > 0.0:
                w_eff = math.log1p(w_raw)
                nbrs.append((v, w_eff))
                s_u += w_eff
        adj[u] = nbrs
        strength[u] = s_u
        heapq.heappush(heap, (s_u, u))

    removed: set[int] = set()
    s_core: dict[int, float] = {}
    current_shell = 0.0

    while heap and len(removed) < len(nodes):
        cur_s, u = heapq.heappop(heap)
        if u in removed:
            continue
        removed.add(u)
        if cur_s > current_shell:
            current_shell = cur_s
        s_core[u] = round(current_shell, 3)

        for v, w_uv in adj[u]:
            if v not in removed:
                new_s = max(0.0, strength[v] - w_uv)
                strength[v] = new_s
                heapq.heappush(heap, (new_s, v))

    return s_core


def _build_directed_csr(directed: nx.DiGraph, node_ids: Sequence[int]) -> sp.csr_matrix | None:
    n = len(node_ids)
    if n == 0 or directed.number_of_edges() == 0:
        return None
    idx_map = {u: i for i, u in enumerate(node_ids)}
    rows: list[int] = []
    cols: list[int] = []
    vals: list[float] = []
    for u, v, data in directed.edges(data=True):
        w = float(data.get("weight", 0.0))
        if w > 0.0 and u in idx_map and v in idx_map:
            rows.append(idx_map[u])
            cols.append(idx_map[v])
            vals.append(w)
    if not vals:
        return None
    return sp.csr_matrix(
        (np.array(vals, dtype=np.float64), (np.array(rows, dtype=np.int32), np.array(cols, dtype=np.int32))),
        shape=(n, n),
    )


def compute_bipartite_salsa(
    directed: nx.DiGraph,
    node_ids: Sequence[int],
    *,
    damping: float = 0.85,
    max_iter: int = 30,
    precomputed_w: sp.csr_matrix | None = None,
) -> tuple[dict[int, float], dict[int, float]]:
    """Vectorized sparse SALSA random walk (Gupta et al., WWW 2013) returning (authority, scout)."""
    n = len(node_ids)
    if n == 0:
        return {}, {}

    w_mat = precomputed_w if precomputed_w is not None else _build_directed_csr(directed, node_ids)
    if w_mat is None:
        zeros = {u: 0.0 for u in node_ids}
        return zeros, zeros.copy()

    out_deg = np.asarray(w_mat.sum(axis=1)).ravel()
    in_deg = np.asarray(w_mat.sum(axis=0)).ravel()

    inv_out = np.where(out_deg > 0.0, 1.0 / np.maximum(out_deg, 1e-12), 0.0)
    inv_in = np.where(in_deg > 0.0, 1.0 / np.maximum(in_deg, 1e-12), 0.0)

    w_r = sp.diags(inv_out) @ w_mat  # row-stochastic hub -> authority
    w_c = w_mat @ sp.diags(inv_in)   # col-stochastic authority -> hub (columns sum to 1)
    w_r_t = w_r.T.tocsr()

    active_auth = in_deg > 0.0
    active_hub = out_deg > 0.0

    # Initialize proportional to in-strength / out-strength on active bipartite sides
    if np.any(active_auth):
        a = in_deg / max(float(in_deg.sum()), 1e-12)
        p_auth = a.copy()
    else:
        a = np.zeros(n, dtype=np.float64)
        p_auth = np.zeros(n, dtype=np.float64)

    if np.any(active_hub):
        h = out_deg / max(float(out_deg.sum()), 1e-12)
        p_hub = h.copy()
    else:
        h = np.zeros(n, dtype=np.float64)
        p_hub = np.zeros(n, dtype=np.float64)

    for _ in range(max_iter):
        # Authority two-step walk: a -> h_mid = W_c * a -> a_next = W_r^T * h_mid
        h_mid = w_c @ a
        step_a = w_r_t @ h_mid
        rem_a = max(0.0, 1.0 - float(step_a.sum()))
        a_next = damping * (step_a + rem_a * p_auth) + (1.0 - damping) * p_auth

        # Hub/Scout two-step walk: h -> a_mid = W_r^T * h -> h_next = W_c * a_mid
        a_mid = w_r_t @ h
        step_h = w_c @ a_mid
        rem_h = max(0.0, 1.0 - float(step_h.sum()))
        h_next = damping * (step_h + rem_h * p_hub) + (1.0 - damping) * p_hub

        diff = float(np.abs(a_next - a).sum() + np.abs(h_next - h).sum())
        a, h = a_next, h_next
        if diff < 1e-7:
            break

    # Zero out nodes with strictly zero inbound / zero outbound edges (ghost isolation)
    a = np.where(active_auth, np.maximum(a, 0.0), 0.0)
    h = np.where(active_hub, np.maximum(h, 0.0), 0.0)

    def _scale_01(vec: np.ndarray, active_mask: np.ndarray) -> np.ndarray:
        if not np.any(active_mask):
            return np.zeros_like(vec)
        # Log-dampen heavy skew while keeping 0 -> 0 and top node -> 1.0
        scaled = np.log1p(vec * n * 4.0)
        hi = float(np.percentile(scaled[active_mask], 98))
        if hi <= 1e-12:
            hi = float(scaled.max())
        if hi <= 1e-12:
            return np.zeros_like(vec)
        return np.clip(np.where(active_mask, scaled / hi, 0.0), 0.0, 1.0)

    a_norm = _scale_01(a, active_auth)
    h_norm = _scale_01(h, active_hub)

    return (
        {node_ids[i]: round(float(a_norm[i]), 4) for i in range(n)},
        {node_ids[i]: round(float(h_norm[i]), 4) for i in range(n)},
    )


def compute_pagerank_state(
    directed: nx.DiGraph,
    node_ids: Sequence[int],
    *,
    damping: float = 0.85,
    max_iter: int = 35,
    precomputed_w: sp.csr_matrix | None = None,
) -> tuple[dict[int, float], dict[int, float], dict[int, float]]:
    """Computes behavioral PageRank distribution, out-degrees, and UserCredV2 scores."""
    n = len(node_ids)
    if n == 0:
        return {}, {}, {}

    w_mat = precomputed_w if precomputed_w is not None else _build_directed_csr(directed, node_ids)
    if w_mat is None:
        uniform = 1.0 / float(n)
        return (
            {u: uniform for u in node_ids},
            {u: 0.0 for u in node_ids},
            {u: 0.0 for u in node_ids},
        )
    out_deg = np.asarray(w_mat.sum(axis=1)).ravel()
    in_deg = np.asarray(w_mat.sum(axis=0)).ravel()

    inv_out = np.where(out_deg > 0.0, 1.0 / np.maximum(out_deg, 1e-12), 0.0)
    trans_t = (sp.diags(inv_out) @ w_mat).T.tocsr()
    dangling_mask = out_deg <= 0.0

    # Behavioral personalization: proportional to inbound activity + minimal floor
    mean_in = max(float(in_deg.mean()), 1.0)
    pers = in_deg + 1e-4 * mean_in
    pers = pers / float(pers.sum())

    pr = pers.copy()
    for _ in range(max_iter):
        dangling_mass = float(pr[dangling_mask].sum())
        pr_next = damping * (trans_t @ pr + dangling_mass * pers) + (1.0 - damping) * pers
        if float(np.abs(pr_next - pr).sum()) < 1e-8:
            pr = pr_next
            break
        pr = pr_next

    pr_map: dict[int, float] = {}
    out_map: dict[int, float] = {}
    scores: dict[int, float] = {}
    for i, u in enumerate(node_ids):
        pr_map[u] = float(pr[i])
        out_map[u] = float(out_deg[i])
        if in_deg[i] <= 0.0 and out_deg[i] <= 0.0:
            scores[u] = 0.0
        else:
            mass = float(pr[i]) * (float(n) / GLOBAL_MASS_EQUIV_NODES)
            scores[u] = round(UserCredV2.from_mass(u, mass).score, 2)
    return pr_map, out_map, scores


def compute_pagerank_user_cred(
    directed: nx.DiGraph,
    node_ids: Sequence[int],
    *,
    damping: float = 0.85,
    max_iter: int = 35,
    precomputed_w: sp.csr_matrix | None = None,
) -> dict[int, float]:
    """Computes behavioral PageRank mass and maps through exact UserCredV2.from_mass."""
    _, _, scores = compute_pagerank_state(
        directed,
        node_ids,
        damping=damping,
        max_iter=max_iter,
        precomputed_w=precomputed_w,
    )
    return scores


def _compute_catalysis_lifts(
    accounts_by_id: dict[int, AccountNodeInput],
    posts: Sequence[PostTrajectoryInput],
    post_interactions: Sequence[PostInteractionInput],
    reference_time: datetime,
) -> tuple[dict[int, float], dict[int, list[tuple[int, float]]]]:
    posts_by_id = {p.id: p for p in posts}
    events_by_actor: dict[int, list[tuple[int, float]]] = {u: [] for u in accounts_by_id}
    seen_actor_post: set[tuple[int, int]] = set()

    for pi in post_interactions:
        u = pi.actor_account_id
        if u is None or u not in accounts_by_id:
            continue
        etype = pi.interaction_type.strip().lower()
        if etype not in ("reply", "quote"):
            continue
        post = posts_by_id.get(pi.post_id)
        if post is None or post.author_account_id == u:
            continue
        if (u, post.id) in seen_actor_post:
            continue
        seen_actor_post.add((u, post.id))

        author = accounts_by_id.get(post.author_account_id)
        author_foll = author.followers_count if author else 10_000
        author_base = author.baseline_p50 if author else {}

        created_dt = _ensure_utc(post.created_at_x, reference_time)
        occ_dt = _ensure_utc(pi.occurred_at, created_dt)
        a0 = max(0.0, (occ_dt - created_dt).total_seconds() / 60.0)
        snaps = sorted(post.snapshots, key=lambda s: s[0])

        lift_ratio: float
        pre_snaps = [s for s in snaps if s[0] <= a0 + 5.0]
        post_snaps = [s for s in snaps if s[0] > (pre_snaps[-1][0] if pre_snaps else a0)]

        if pre_snaps and post_snaps:
            s_pre = pre_snaps[-1]
            s_post = min(post_snaps, key=lambda s: abs(s[0] - (a0 + 60.0)))
            obs_delta = max(0.0, float(s_post[1] - s_pre[1]))
            exp_delta = max(
                20.0,
                interpolate_baseline_p50(author_base, author_foll, s_post[0])
                - interpolate_baseline_p50(author_base, author_foll, s_pre[0]),
            )
            lift_ratio = obs_delta / exp_delta
        elif post_snaps:
            s_post = min(post_snaps, key=lambda s: abs(s[0] - (a0 + 60.0)))
            exp_v = max(25.0, interpolate_baseline_p50(author_base, author_foll, s_post[0]))
            lift_ratio = float(s_post[1]) / exp_v
        else:
            cur_age = max(15.0, (reference_time - created_dt).total_seconds() / 60.0)
            exp_v = max(
                25.0,
                interpolate_baseline_p50(author_base, author_foll, min(cur_age, 2880.0)),
            )
            lift_ratio = float(post.latest_views) / exp_v

        clamped_ratio = min(10.0, max(0.25, lift_ratio))
        events_by_actor[u].append((post.id, round(clamped_ratio, 2)))

    # Empirical-Bayes smoothed lift with neutral 1.0x prior when 0 events exist
    prior_strength = 0.6
    lifts: dict[int, float] = {}
    for u, evs in events_by_actor.items():
        if not evs:
            lifts[u] = 1.0
        else:
            sum_r = sum(r for _, r in evs)
            smoothed = (1.0 * prior_strength + sum_r) / (prior_strength + len(evs))
            lifts[u] = round(min(10.0, max(0.25, smoothed)), 2)

    return lifts, events_by_actor


def compute_heavy_hitters(
    accounts: Sequence[AccountNodeInput],
    bundle: BehavioralGraphBundle,
    posts: Sequence[PostTrajectoryInput] = (),
    post_interactions: Sequence[PostInteractionInput] = (),
    interested_in: dict[int, dict[int, float]] | None = None,
    known_for: dict[int, dict[int, float]] | None = None,
    *,
    reference_time: datetime | None = None,
) -> HeavyHittersResult:
    """Computes 0-100 Heavy Hitter Score overall and per community."""
    now = _ensure_utc(reference_time, datetime.now(timezone.utc))
    node_ids = bundle.node_ids
    if not node_ids:
        return HeavyHittersResult(by_account={}, catalyzed_events={})

    accounts_by_id = {a.id: a for a in accounts}
    lifts, catalyzed_events = _compute_catalysis_lifts(
        accounts_by_id, posts, post_interactions, now
    )
    s_cores = compute_weighted_s_core(bundle.undirected)
    shared_w = _build_directed_csr(bundle.directed, node_ids)
    salsa_auth, salsa_scout = compute_bipartite_salsa(
        bundle.directed, node_ids, precomputed_w=shared_w
    )
    pr_map, out_deg_map, user_creds = compute_pagerank_state(
        bundle.directed, node_ids, precomputed_w=shared_w
    )

    # Own-post tracer reach efficiency per account
    posts_by_author: dict[int, list[PostTrajectoryInput]] = {u: [] for u in node_ids}
    for p in posts:
        if (not p.is_reply) and (not p.is_retweet) and p.author_account_id in posts_by_author:
            posts_by_author[p.author_account_id].append(p)

    own_reach_norm: dict[int, float] = {}
    for u in node_ids:
        acct = accounts_by_id[u]
        own_posts = posts_by_author.get(u, [])
        if not own_posts:
            # Neutral-low baseline; if totally isolated in graph, stays near 0
            has_edges = bundle.undirected.degree(u) > 0
            own_reach_norm[u] = 0.25 if has_edges else 0.0
        else:
            vm_vals = [max(0.0, float(p.value_model_score)) for p in own_posts]
            med_vm = statistics.median(vm_vals)
            foll_denom = max(500.0, float(acct.followers_count))
            reach_mults = [float(p.latest_views) / foll_denom for p in own_posts]
            med_mult = statistics.median(reach_mults)
            vm_part = min(1.0, med_vm / 1.5)
            mult_part = min(1.0, med_mult / 0.55)
            own_reach_norm[u] = round(0.55 * vm_part + 0.45 * mult_part, 4)

    max_s_core = max(max(s_cores.values(), default=0.0), 1.0)

    # Collect all community IDs if provided
    all_comm_ids: set[int] = set()
    if interested_in:
        for cmap in interested_in.values():
            all_comm_ids.update(cmap.keys())

    by_account: dict[int, AccountHeavyHitterMetrics] = {}
    for u in node_ids:
        acct = accounts_by_id[u]
        cat_lift = lifts.get(u, 1.0)
        cat_cnt = len(catalyzed_events.get(u, []))
        sc = s_cores.get(u, 0.0)
        sa = salsa_auth.get(u, 0.0)
        ss = salsa_scout.get(u, 0.0)
        uc = user_creds.get(u, 0.0)
        own_r = own_reach_norm.get(u, 0.0)

        # Normalize catalysis lift: 1.0x neutral -> ~0.18, 3.5x+ -> 1.0; isolated ghost -> 0.0
        is_isolated = bundle.undirected.degree(u) == 0 and cat_cnt == 0
        if is_isolated:
            cat_norm = 0.0
        else:
            cat_norm = min(1.0, max(0.0, (cat_lift - 0.55) / 2.95))

        sc_norm = min(1.0, sc / max_s_core)
        uc_norm = min(1.0, max(0.0, uc / 100.0))

        raw_hh = 100.0 * (
            0.30 * cat_norm
            + 0.22 * sc_norm
            + 0.22 * sa
            + 0.14 * uc_norm
            + 0.12 * own_r
        )
        hh_score = round(min(100.0, max(0.0, raw_hh)), 1)

        # Follower efficiency: rewards punch-above-weight creators & penalizes bloated ghosts
        log_foll = max(1.0, math.log10(max(acct.followers_count, 100)) - 1.5)
        foll_eff = round(hh_score / log_foll, 2)

        comm_scores: dict[int, float] = {}
        for cid in all_comm_ids:
            aff_int = (interested_in or {}).get(u, {}).get(cid, 0.0)
            aff_kn = (known_for or {}).get(u, {}).get(cid, 0.0)
            c_aff = max(aff_int, 0.85 * aff_kn)
            # Scale Heavy Hitter impact within community c by structural presence in c
            comm_scores[cid] = round(min(100.0, hh_score * (0.35 + 0.65 * min(1.0, c_aff * 1.35))), 1)

        by_account[u] = AccountHeavyHitterMetrics(
            heavy_hitter_score=hh_score,
            catalysis_lift=cat_lift,
            catalyzed_posts_count=cat_cnt,
            s_core_depth=sc,
            salsa_authority=sa,
            salsa_scout=ss,
            user_cred_v2=uc,
            own_post_reach_norm=own_r,
            follower_efficiency=foll_eff,
            community_scores=comm_scores,
        )

    return HeavyHittersResult(
        by_account=by_account,
        catalyzed_events=catalyzed_events,
        pr_by_account=pr_map,
        out_deg_by_account=out_deg_map,
    )
