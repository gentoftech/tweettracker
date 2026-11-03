"""Greedy Determinantal Point Process (DPP) diversity scorer matching vm-ranker/dpp_model.rs."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import hashlib
import math
import re

import numpy as np

DEFAULT_DPP_THETA: float = 0.65
DEFAULT_DPP_MAX_SELECTED_RANK: int = 150

_TOKEN_RE = re.compile(r"[a-z0-9#@_]{2,}")


def l2_normalize(vec: Sequence[float]) -> np.ndarray:
    arr = np.asarray(vec, dtype=np.float64)
    if arr.ndim != 1 or arr.size == 0:
        return np.zeros(0, dtype=np.float64)
    norm = float(np.linalg.norm(arr))
    if norm <= 1e-12:
        unit = np.zeros_like(arr)
        unit[0] = 1.0
        return unit
    return arr / norm


def compute_text_embedding(text: str, dim: int = 64) -> list[float]:
    """Deterministic signed feature-hashed word + trigram L2-normalized embedding."""
    if dim <= 0:
        raise ValueError("dim must be positive")
    vec = np.zeros(dim, dtype=np.float64)
    cleaned = text.lower().strip()
    tokens = _TOKEN_RE.findall(cleaned)
    features: list[tuple[str, float]] = []
    for tok in tokens:
        features.append((f"w:{tok}", 1.5))
        if len(tok) >= 4:
            for idx in range(len(tok) - 2):
                features.append((f"c3:{tok[idx : idx + 3]}", 0.5))
    for idx in range(len(tokens) - 1):
        features.append((f"bi:{tokens[idx]}_{tokens[idx + 1]}", 1.2))

    if not features:
        vec[0] = 1.0
        return vec.tolist()

    for feat, weight in features:
        digest = hashlib.blake2b(feat.encode("utf-8"), digest_size=8).digest()
        h = int.from_bytes(digest, "little")
        bucket = h % dim
        sign = 1.0 if ((h >> 32) & 1) == 0 else -1.0
        vec[bucket] += sign * weight

    return [round(float(x), 6) for x in l2_normalize(vec)]


@dataclass(slots=True)
class DppCandidate:
    id: int | str
    score: float
    embedding: Sequence[float]


@dataclass(slots=True)
class DppRescoreItem:
    id: int | str
    original_score: float
    rescored_score: float
    novelty_multiplier: float
    selected: bool
    rank: int | None


def dpp_novelty_multiplier(
    candidate_embedding: Sequence[float],
    reference_embeddings: Sequence[Sequence[float]],
    theta: float = DEFAULT_DPP_THETA,
) -> float:
    """Computes DPP Schur-complement novelty multiplier (d_i^2)^(1 - theta) in [0, 1]
    over L2-normalized embeddings. Returns 1.0 when orthogonal/empty, -> 0.0 when duplicate.
    """
    t_val = float(theta)
    if not math.isfinite(t_val):
        raise ValueError("dpp theta must be finite.")
    clamped_theta = min(max(t_val, 0.0), 1.0)

    cand = l2_normalize(candidate_embedding)
    if cand.size == 0 or not reference_embeddings:
        return 1.0

    valid_refs = [
        l2_normalize(ref)
        for ref in reference_embeddings
        if len(ref) == cand.size
    ]
    if not valid_refs:
        return 1.0

    # Rank-revealing SVD basis prevents collinear/duplicate references from producing
    # garbage orthogonal vectors that would double-subtract projections and inflate residual_sq.
    mat = np.column_stack(valid_refs)
    u_mat, s_vals, _ = np.linalg.svd(mat, full_matrices=False)
    rank_mask = s_vals > 1e-6
    if not np.any(rank_mask):
        return 1.0
    q_basis = u_mat[:, rank_mask]

    proj = q_basis @ (q_basis.T @ cand)
    residual_sq = float(np.dot(cand - proj, cand - proj))
    residual_sq = min(max(residual_sq, 0.0), 1.0)

    if residual_sq <= 1e-12:
        return 0.0
    return float(math.pow(residual_sq, 1.0 - clamped_theta))


def dpp_greedy_rescore(
    candidates: Sequence[DppCandidate],
    theta: float = DEFAULT_DPP_THETA,
    max_selected_rank: int = DEFAULT_DPP_MAX_SELECTED_RANK,
    min_residual_det: float = 0.12,
) -> list[DppRescoreItem]:
    """Fast Greedy MAP Determinantal Point Process rescorer matching vm-ranker/dpp_model.rs:
    - Sorts candidates by initial score descending and takes up to `max_selected_rank` (150).
    - Iteratively selects candidates maximizing `theta * log(score) + 0.5 * (1 - theta) * log(d_i^2)`
      via incremental Cholesky updates on the cosine similarity kernel.
    - Candidates failing semantic orthogonality (`d_i^2 < min_residual_det`) or outside
      `max_selected_rank` are dropped (`rescored_score = 0.0`, `selected = False`) per dpp_model.rs.
    """
    n_total = len(candidates)
    if n_total == 0:
        return []

    t_val = float(theta)
    if not math.isfinite(t_val):
        raise ValueError("dpp theta must be finite.")
    clamped_theta = min(max(t_val, 0.0), 0.999999)

    indexed = list(enumerate(candidates))
    indexed.sort(key=lambda pair: (-pair[1].score, pair[0]))
    pool = [(orig_idx, c) for orig_idx, c in indexed if c.score > 0.0][: max(0, max_selected_rank)]
    m = len(pool)

    if m == 0:
        return [
            DppRescoreItem(
                id=c.id,
                original_score=c.score,
                rescored_score=0.0,
                novelty_multiplier=1.0,
                selected=False,
                rank=None,
            )
            for c in candidates
        ]

    dim = len(pool[0][1].embedding) or 16
    emb_matrix = np.zeros((m, dim), dtype=np.float64)
    rel_scores = np.zeros(m, dtype=np.float64)
    for k, (_, c) in enumerate(pool):
        if len(c.embedding) == dim:
            emb_matrix[k] = l2_normalize(c.embedding)
        else:
            unit = np.zeros(dim, dtype=np.float64)
            unit[k % dim] = 1.0
            emb_matrix[k] = unit
        rel_scores[k] = max(float(c.score), 1e-12)

    sim_kernel = emb_matrix @ emb_matrix.T
    np.fill_diagonal(sim_kernel, 1.0)

    c_vecs = np.zeros((m, m), dtype=np.float64)
    di2 = np.ones(m, dtype=np.float64)
    selected_pool_mask = np.zeros(m, dtype=bool)
    novelty_by_pool = np.ones(m, dtype=np.float64)
    rank_by_pool: dict[int, int] = {}

    for step in range(m):
        best_k = -1
        best_obj = -float("inf")
        for k in range(m):
            if selected_pool_mask[k]:
                continue
            if di2[k] < min_residual_det:
                continue
            obj = clamped_theta * math.log(rel_scores[k]) + 0.5 * (1.0 - clamped_theta) * math.log(
                max(di2[k], 1e-15)
            )
            if obj > best_obj:
                best_obj = obj
                best_k = k

        if best_k < 0:
            break

        selected_pool_mask[best_k] = True
        rank_by_pool[best_k] = step + 1
        novelty_by_pool[best_k] = math.pow(max(di2[best_k], 0.0), 1.0 - clamped_theta)

        denom = math.sqrt(max(di2[best_k], 1e-15))
        for j in range(m):
            if selected_pool_mask[j]:
                continue
            dot_prev = float(np.dot(c_vecs[best_k, :step], c_vecs[j, :step])) if step > 0 else 0.0
            e_j = (float(sim_kernel[best_k, j]) - dot_prev) / denom
            c_vecs[j, step] = e_j
            di2[j] = max(0.0, di2[j] - e_j * e_j)

    for k in range(m):
        if not selected_pool_mask[k]:
            novelty_by_pool[k] = math.pow(max(di2[k], 0.0), 1.0 - clamped_theta)

    pool_lookup: dict[int, tuple[bool, float, int | None]] = {}
    for k, (orig_idx, _) in enumerate(pool):
        pool_lookup[orig_idx] = (
            bool(selected_pool_mask[k]),
            float(novelty_by_pool[k]),
            rank_by_pool.get(k),
        )

    results: list[DppRescoreItem] = []
    for orig_idx, cand in enumerate(candidates):
        if orig_idx in pool_lookup:
            is_sel, nov_mult, rnk = pool_lookup[orig_idx]
            rescored = cand.score if is_sel else 0.0
            results.append(
                DppRescoreItem(
                    id=cand.id,
                    original_score=cand.score,
                    rescored_score=rescored,
                    novelty_multiplier=nov_mult,
                    selected=is_sel,
                    rank=rnk,
                )
            )
        else:
            results.append(
                DppRescoreItem(
                    id=cand.id,
                    original_score=cand.score,
                    rescored_score=0.0,
                    novelty_multiplier=0.0,
                    selected=False,
                    rank=None,
                )
            )
    return results
