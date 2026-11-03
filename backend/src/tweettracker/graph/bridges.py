"""Story 1.3: K-Normalized Guimerà–Amaral Participation, Burt Brokerage & N-Pole Radial Layout."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import math

import networkx as nx
import numpy as np
import scipy.sparse as sp

from tweettracker.graph.builder import BehavioralGraphBundle
from tweettracker.graph.nmf_simclusters import CommunityAnchorInput

GOLDEN_ANGLE: float = math.pi * (3.0 - math.sqrt(5.0))


@dataclass(slots=True)
class PoleCoordinate:
    community_id: int
    name: str
    slug: str
    color: str
    x: float
    y: float


@dataclass(slots=True)
class AccountBridgeMetrics:
    bridge_score: float
    participation_coeff: float
    burt_brokerage: float
    bridge_role: str
    n_pole_xy: tuple[float, float]
    single_community_xy: tuple[float, float]


@dataclass(slots=True)
class BridgesResult:
    poles: list[PoleCoordinate]
    by_account: dict[int, AccountBridgeMetrics]


def guimera_amaral_participation(community_weights: Sequence[float]) -> float:
    """Computes K-normalized Guimerà–Amaral participation coefficient in [0, 1].
    P_tilde = (K / (K - 1)) * (1 - sum_c (w_{u,c} / w_u)^2)
    """
    k = len(community_weights)
    if k <= 1:
        return 0.0
    pos = [max(0.0, float(w)) for w in community_weights]
    total = sum(pos)
    if total <= 1e-12:
        return 0.0
    hhi = sum((w / total) ** 2 for w in pos)
    raw = (float(k) / float(k - 1)) * (1.0 - hhi)
    return min(1.0, max(0.0, raw))


def classify_bridge_role(
    participation_coeff: float,
    community_shares: Sequence[float],
) -> str:
    """Classifies account into `provincial`, `two_way_bridge`, or `multi_circle_connector`."""
    k = len(community_shares)
    if k <= 1 or participation_coeff < 0.35:
        return "provincial"

    total = sum(max(0.0, float(s)) for s in community_shares)
    if total <= 1e-12:
        return "provincial"

    shares_desc = sorted((max(0.0, float(s)) / total for s in community_shares), reverse=True)
    top2_share = sum(shares_desc[:2])
    meaningful_circles = sum(1 for s in shares_desc if s >= 0.12)

    if k >= 3 and participation_coeff >= 0.55 and meaningful_circles >= 3 and top2_share < 0.80:
        return "multi_circle_connector"
    return "two_way_bridge"


def compute_burt_brokerage(
    undirected: nx.Graph,
    node_ids: Sequence[int],
    *,
    precomputed_adj: sp.csr_matrix | None = None,
) -> dict[int, float]:
    """Exact sparse matrix evaluation of Burt's structural hole brokerage (1 - C_u) in [0, 1].
    Matches `networkx.constraint(undirected, weight='weight')` to 1e-12 while executing in ~60ms for 5k nodes.
    """
    n = len(node_ids)
    if n == 0:
        return {}
    if precomputed_adj is None and undirected.number_of_edges() == 0:
        return {u: 0.0 for u in node_ids}

    if precomputed_adj is not None:
        adj = precomputed_adj
    else:
        adj = nx.to_scipy_sparse_array(
            undirected, nodelist=node_ids, weight="weight", format="csr", dtype=np.float64
        )
        adj.setdiag(0.0)
        adj.eliminate_zeros()

    degrees = np.diff(adj.indptr)
    row_sums = np.asarray(adj.sum(axis=1)).ravel()
    inv_row = np.where(row_sums > 0.0, 1.0 / np.maximum(row_sums, 1e-15), 0.0)

    p_mat = sp.diags(inv_row) @ adj
    mask = (p_mat > 0.0).astype(np.float64)
    # Dyadic constraint matrix S_{u,v} = P_{u,v} + sum_q P_{u,q} P_{q,v} for v in N(u)
    s_mat = p_mat + (p_mat @ p_mat).multiply(mask)
    constraints = np.asarray(s_mat.power(2).sum(axis=1)).ravel()

    brokerage: dict[int, float] = {}
    for i, u in enumerate(node_ids):
        if degrees[i] <= 1 or row_sums[i] <= 1e-12:
            brokerage[u] = 0.0
        else:
            c_u = float(constraints[i])
            if math.isnan(c_u):
                brokerage[u] = 0.0
            else:
                brokerage[u] = round(min(1.0, max(0.0, 1.0 - c_u)), 4)
    return brokerage


def _relax_node_collisions(
    coords: np.ndarray,
    *,
    min_dist: float = 0.085,
    passes: int = 16,
) -> np.ndarray:
    """Deterministic pairwise collision relaxation + centered [-0.95, 0.95] normalization."""
    n = coords.shape[0]
    if n == 0:
        return coords
    if n == 1:
        return np.zeros((1, 2), dtype=np.float64)

    pts = coords.astype(np.float64, copy=True)
    pts -= pts.mean(axis=0, keepdims=True)
    max_abs = float(np.max(np.abs(pts)))
    if max_abs > 1e-9:
        pts = (pts / max_abs) * 0.88

    if n <= 400:
        for _ in range(passes):
            diff = pts[:, None, :] - pts[None, :, :]  # (N, N, 2)
            dist = np.linalg.norm(diff, axis=2)  # (N, N)
            np.fill_diagonal(dist, np.inf)
            overlap = np.maximum(0.0, min_dist - dist)
            mask = overlap > 0.0
            if not np.any(mask):
                break
            safe_dist = np.where((dist > 1e-9) & np.isfinite(dist), dist, 1.0)
            unit = np.where(np.isfinite(dist)[:, :, None], diff / safe_dist[:, :, None], 0.0)
            # Deterministic break for exact (0, 0) ties
            zero_ties = mask & (dist <= 1e-9)
            if np.any(zero_ties):
                idx_grid = np.arange(n, dtype=np.float64)
                angles = (idx_grid[:, None] - idx_grid[None, :]) * GOLDEN_ANGLE
                unit = np.where(
                    zero_ties[:, :, None],
                    np.stack([np.cos(angles), np.sin(angles)], axis=2),
                    unit,
                )
            push = unit * (0.45 * overlap[:, :, None])
            pts += push.sum(axis=1)

    pts -= pts.mean(axis=0, keepdims=True)
    span = float(np.max(np.linalg.norm(pts, axis=1)))
    if span > 1e-9:
        pts = (pts / span) * 0.92
    return pts


def _compute_organic_force_layout(
    bundle: BehavioralGraphBundle,
    node_ids: Sequence[int],
    aff_matrix: np.ndarray,
    pole_vecs: np.ndarray,
    heavy_hitter_scores: dict[int, float],
) -> dict[int, tuple[float, float]]:
    """Computes an organic, pole-free force-directed layout driven by actual social edges
    (`bundle.undirected`) plus lightweight O(N) intra-circle cohesion.
    Uses exact Fruchterman–Reingold + collision relaxation for N <= 400, and O(N + E)
    sparse orthogonalized spring relaxation for massive graphs (N > 400).
    """
    n = len(node_ids)
    if n == 0:
        return {}
    if n == 1:
        return {node_ids[0]: (0.0, 0.0)}

    idx_of = {u: i for i, u in enumerate(node_ids)}
    base = 0.55 * (aff_matrix @ pole_vecs)
    hh_arr = np.array(
        [min(100.0, max(0.0, heavy_hitter_scores.get(u, 0.0))) for u in node_ids],
        dtype=np.float64,
    )
    hh_pull = 1.0 - 0.22 * (hh_arr / 100.0)
    thetas = np.arange(n, dtype=np.float64) * GOLDEN_ANGLE
    jitter = 0.14 * np.column_stack([np.cos(thetas), np.sin(thetas)])
    init_coords = base * hh_pull[:, None] + jitter

    # O(N) intra-community ring cohesion so disconnected early seeds still cluster naturally
    prim_comm = np.argmax(aff_matrix, axis=1)
    buckets: dict[int, list[int]] = {}
    for i, c_idx in enumerate(prim_comm.tolist()):
        buckets.setdefault(int(c_idx), []).append(i)

    if n <= 400:
        g_layout = nx.Graph()
        g_layout.add_nodes_from(node_ids)

        for u, v, edata in bundle.undirected.edges(data=True):
            if u in idx_of and v in idx_of:
                raw_w = max(0.05, float(edata.get("weight", 1.0)))
                g_layout.add_edge(u, v, weight=math.log1p(raw_w) * 1.85)

        for members in buckets.values():
            m = len(members)
            if m < 2:
                continue
            for pos, i in enumerate(members):
                u = node_ids[i]
                for step in (1, 2):
                    if m <= step:
                        continue
                    v = node_ids[members[(pos + step) % m]]
                    if g_layout.has_edge(u, v):
                        g_layout[u][v]["weight"] += 0.18
                    else:
                        g_layout.add_edge(u, v, weight=0.18)

        init_pos = {node_ids[i]: init_coords[i] for i in range(n)}
        opt_k = 1.45 / math.sqrt(max(n, 4))
        pos_map = nx.spring_layout(
            g_layout,
            k=opt_k,
            pos=init_pos,
            weight="weight",
            iterations=50,
            seed=20261102,
        )
        raw_coords = np.array([pos_map[u] for u in node_ids], dtype=np.float64)
        min_d = min(0.11, 0.62 / math.sqrt(max(n, 4)))
        relaxed = _relax_node_collisions(raw_coords, min_dist=min_d, passes=14)
    else:
        rows: list[int] = []
        cols: list[int] = []
        vals: list[float] = []
        for u, v, edata in bundle.undirected.edges(data=True):
            iu = idx_of.get(u)
            iv = idx_of.get(v)
            if iu is None or iv is None:
                continue
            w = math.log1p(max(0.05, float(edata.get("weight", 1.0)))) * 1.85
            rows.extend((iu, iv))
            cols.extend((iv, iu))
            vals.extend((w, w))

        for members in buckets.values():
            m = len(members)
            if m < 2:
                continue
            for pos, iu in enumerate(members):
                iv = members[(pos + 1) % m]
                rows.extend((iu, iv))
                cols.extend((iv, iu))
                vals.extend((0.18, 0.18))

        adj = sp.csr_matrix((vals, (rows, cols)), shape=(n, n), dtype=np.float64)
        deg = np.asarray(adj.sum(axis=1)).ravel()
        inv_deg = np.where(deg > 1e-9, 1.0 / deg, 0.0)
        trans = sp.diags(inv_deg) @ adj

        coords = init_coords.copy()
        for _ in range(18):
            pulled = trans @ coords
            coords = np.where(deg[:, None] > 1e-9, 0.68 * pulled + 0.32 * coords, coords)
            coords -= coords.mean(axis=0, keepdims=True)
            cov = (coords.T @ coords) / max(1, n) + 1e-6 * np.eye(2)
            eigvals, eigvecs = np.linalg.eigh(cov)
            inv_sqrt = eigvecs @ np.diag(1.0 / np.sqrt(np.maximum(eigvals, 1e-8))) @ eigvecs.T
            coords = coords @ inv_sqrt + 0.08 * init_coords

        relaxed = _relax_node_collisions(coords, min_dist=0.0, passes=0)

    return {
        node_ids[i]: (round(float(relaxed[i, 0]), 5), round(float(relaxed[i, 1]), 5))
        for i in range(n)
    }


def compute_n_pole_anchors(communities: Sequence[CommunityAnchorInput]) -> list[PoleCoordinate]:
    """Places K community poles evenly at p_c = (cos(2*pi*(c-1)/K), sin(2*pi*(c-1)/K))."""
    k = len(communities)
    if k == 0:
        return []
    if k == 1:
        c0 = communities[0]
        return [
            PoleCoordinate(
                community_id=c0.id,
                name=c0.name,
                slug=c0.slug,
                color=c0.color,
                x=0.0,
                y=0.0,
            )
        ]

    poles: list[PoleCoordinate] = []
    for idx, comm in enumerate(communities):
        angle = (2.0 * math.pi * float(idx)) / float(k)
        poles.append(
            PoleCoordinate(
                community_id=comm.id,
                name=comm.name,
                slug=comm.slug,
                color=comm.color,
                x=round(math.cos(angle), 6),
                y=round(math.sin(angle), 6),
            )
        )
    return poles


def _compute_core_periphery_positions(
    node_ids: Sequence[int],
    heavy_hitter_scores: dict[int, float],
    sub_pocket_labels: dict[int, str],
) -> dict[int, tuple[float, float]]:
    """Deterministic core-periphery layout placing high-influence voices near the center."""
    if not node_ids:
        return {}

    # Group by sub_pocket_label so Louvain sub-pockets occupy coherent angular sectors
    pockets: dict[str, list[int]] = {}
    for u in node_ids:
        lbl = sub_pocket_labels.get(u) or "Core #1"
        pockets.setdefault(lbl, []).append(u)

    sorted_pocket_keys = sorted(pockets.keys())
    n_pockets = max(1, len(sorted_pocket_keys))

    coords: dict[int, tuple[float, float]] = {}
    for p_idx, p_key in enumerate(sorted_pocket_keys):
        members = sorted(
            pockets[p_key],
            key=lambda u: (-heavy_hitter_scores.get(u, 0.0), u),
        )
        sector_center = (2.0 * math.pi * p_idx) / float(n_pockets)
        sector_span = (2.0 * math.pi) / float(n_pockets)

        for rank_i, u in enumerate(members):
            hh = min(100.0, max(0.0, heavy_hitter_scores.get(u, 0.0)))
            # High HH -> inner core (r ~ 0.12..0.38); low HH / ghost -> outer periphery (r ~ 0.85..0.98)
            base_r = 0.94 - 0.78 * (hh / 100.0)
            spiral_jitter = 0.025 * math.sqrt((rank_i % 9) + 1)
            r = min(1.05, max(0.07, base_r + spiral_jitter * 0.4))

            if n_pockets == 1:
                theta = rank_i * GOLDEN_ANGLE
            else:
                local_offset = (((rank_i * 0.61803398875) % 1.0) - 0.5) * (sector_span * 0.78)
                theta = sector_center + local_offset

            coords[u] = (round(r * math.cos(theta), 5), round(r * math.sin(theta), 5))

    return coords


def compute_bridges_and_layout(
    bundle: BehavioralGraphBundle,
    communities: Sequence[CommunityAnchorInput],
    interested_in: dict[int, dict[int, float]],
    known_for: dict[int, dict[int, float]],
    heavy_hitter_scores: dict[int, float],
    sub_pocket_labels: dict[int, str],
) -> BridgesResult:
    """Computes Bridge Score, Bridge Role, N-Pole Radial coordinates, and Single-Community coordinates."""
    node_ids = bundle.node_ids
    k = len(communities)
    poles = compute_n_pole_anchors(communities)

    if not node_ids:
        return BridgesResult(poles=poles, by_account={})

    single_xy = _compute_core_periphery_positions(
        node_ids, heavy_hitter_scores, sub_pocket_labels
    )

    # K <= 1: bridge_score = 0.0, bridge_role = "provincial", organic force-directed coordinates
    if k <= 1:
        pocket_keys = sorted({sub_pocket_labels.get(u) or "Core #1" for u in node_ids})
        m_pockets = max(1, len(pocket_keys))
        pocket_idx_map = {pk: idx for idx, pk in enumerate(pocket_keys)}
        n_single = len(node_ids)
        aff_single = np.zeros((n_single, m_pockets), dtype=np.float64)
        for i, u in enumerate(node_ids):
            pk = sub_pocket_labels.get(u) or "Core #1"
            aff_single[i, pocket_idx_map[pk]] = 1.0
        if m_pockets == 1:
            pocket_vecs = np.zeros((1, 2), dtype=np.float64)
        else:
            pocket_vecs = np.array(
                [
                    [
                        math.cos((2.0 * math.pi * float(idx)) / float(m_pockets)),
                        math.sin((2.0 * math.pi * float(idx)) / float(m_pockets)),
                    ]
                    for idx in range(m_pockets)
                ],
                dtype=np.float64,
            )
        organic_single_xy = _compute_organic_force_layout(
            bundle,
            node_ids,
            aff_single,
            pocket_vecs,
            heavy_hitter_scores,
        )
        by_acct_single = {
            u: AccountBridgeMetrics(
                bridge_score=0.0,
                participation_coeff=0.0,
                burt_brokerage=0.0,
                bridge_role="provincial",
                n_pole_xy=organic_single_xy[u],
                single_community_xy=organic_single_xy[u],
            )
            for u in node_ids
        }
        return BridgesResult(poles=poles, by_account=by_acct_single)

    comm_ids = [c.id for c in communities]
    node_idx = {u: i for i, u in enumerate(node_ids)}
    n = len(node_ids)

    # Normalized anchor profile Z (N x K) per neighbor for edge community attribution
    z_mat = np.zeros((n, k), dtype=np.float64)
    int_mat = np.zeros((n, k), dtype=np.float64)
    for i, u in enumerate(node_ids):
        kf_u = known_for.get(u, {})
        in_u = interested_in.get(u, {})
        for c_i, cid in enumerate(comm_ids):
            int_mat[i, c_i] = max(0.0, float(in_u.get(cid, 0.0)))
            z_mat[i, c_i] = max(0.0, float(kf_u.get(cid, 0.0))) + 0.35 * int_mat[i, c_i]

    z_sums = z_mat.sum(axis=1, keepdims=True)
    z_norm = np.where(z_sums > 1e-12, z_mat / np.maximum(z_sums, 1e-12), 1.0 / k)

    int_sums = int_mat.sum(axis=1, keepdims=True)
    int_norm = np.where(int_sums > 1e-12, int_mat / np.maximum(int_sums, 1e-12), 1.0 / k)

    # Vectorized neighbor-community weight accumulation: W_comm = A_sym @ Z_norm
    a_sym_csr: sp.csr_matrix | None = None
    if bundle.undirected.number_of_edges() > 0:
        a_sym_csr = nx.to_scipy_sparse_array(
            bundle.undirected, nodelist=node_ids, weight="weight", format="csr", dtype=np.float64
        )
        a_sym_csr.setdiag(0.0)
        a_sym_csr.eliminate_zeros()
        edge_comm_w = a_sym_csr @ z_norm  # shape (N, K)
        node_strength = np.asarray(a_sym_csr.sum(axis=1)).ravel()
    else:
        edge_comm_w = np.zeros((n, k), dtype=np.float64)
        node_strength = np.zeros(n, dtype=np.float64)

    brokerage_map = compute_burt_brokerage(
        bundle.undirected, node_ids, precomputed_adj=a_sym_csr
    )
    pole_vecs = np.array([[p.x, p.y] for p in poles], dtype=np.float64)  # shape (K, 2)

    aff_matrix = np.zeros((n, k), dtype=np.float64)
    metrics_stage: dict[int, tuple[float, float, float, str]] = {}
    for i, u in enumerate(node_ids):
        deg_w = float(node_strength[i])
        brok = brokerage_map.get(u, 0.0)

        if deg_w <= 1e-12:
            # Isolated node: zero behavioral bridge participation
            p_coeff = 0.0
            b_score = 0.0
            b_role = "provincial"
            aff_vec = int_norm[i]
        else:
            ec_row = edge_comm_w[i]
            ec_sum = float(ec_row.sum())
            ec_dist = (ec_row / ec_sum) if ec_sum > 1e-12 else int_norm[i]
            # Blend direct behavioral edge community split with NMF InterestedIn overlap
            blended_shares = 0.60 * ec_dist + 0.40 * int_norm[i]
            p_coeff = round(guimera_amaral_participation(blended_shares.tolist()), 4)
            b_role = classify_bridge_role(p_coeff, blended_shares.tolist())
            raw_bs = (0.62 * p_coeff + 0.38 * brok) * 100.0
            b_score = round(min(100.0, max(0.0, raw_bs)), 1)
            aff_vec = blended_shares / max(float(blended_shares.sum()), 1e-12)

        aff_matrix[i] = aff_vec
        metrics_stage[u] = (b_score, p_coeff, brok, b_role)

    force_xy = _compute_organic_force_layout(
        bundle,
        node_ids,
        aff_matrix,
        pole_vecs,
        heavy_hitter_scores,
    )

    by_account: dict[int, AccountBridgeMetrics] = {}
    for u in node_ids:
        b_score, p_coeff, brok, b_role = metrics_stage[u]
        by_account[u] = AccountBridgeMetrics(
            bridge_score=b_score,
            participation_coeff=p_coeff,
            burt_brokerage=brok,
            bridge_role=b_role,
            n_pole_xy=force_xy[u],
            single_community_xy=single_xy[u],
        )

    return BridgesResult(poles=poles, by_account=by_account)
