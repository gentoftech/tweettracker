"""Exact ports of OONRetweetReplyFilter, SelfReplyChainFilter, AgeFilter, and AuthorColdStart."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
import numpy as np


@dataclass(slots=True)
class FilterCandidate:
    """Normalized candidate representation for home-mixer pre-scoring filters & cold-start."""

    tweet_id: int
    author_id: int = 0
    in_network: bool | None = None
    in_reply_to_tweet_id: int | None = None
    retweeted_tweet_id: int | None = None
    conversation_id: int | None = None
    ancestors: list[int] = field(default_factory=list)
    ancestor_users: list[int] = field(default_factory=list)
    age_secs: float | None = None
    author_followers_count: int | None = None
    view_count_on_home: int | None = None
    fav_count: int | None = None
    is_phoenix_home_retrieval: bool = False


@dataclass(slots=True)
class FilterResult:
    kept: list[FilterCandidate]
    removed: list[FilterCandidate]


class OONRetweetReplyFilter:
    """Exact port of home-mixer/filters/oon_retweet_reply_filter.rs (9106e9f)."""

    def __init__(
        self,
        enable_phoenix_oon_replies: bool = False,
        phoenix_oon_replies_root_only: bool = False,
    ) -> None:
        self.enable_phoenix_oon_replies = enable_phoenix_oon_replies
        self.phoenix_oon_replies_root_only = phoenix_oon_replies_root_only

    def filter(self, candidates: Sequence[FilterCandidate]) -> FilterResult:
        kept: list[FilterCandidate] = []
        removed: list[FilterCandidate] = []
        for c in candidates:
            is_reply = c.in_reply_to_tweet_id is not None
            is_retweet = c.retweeted_tweet_id is not None
            replies_to_root = (
                c.conversation_id is not None and c.in_reply_to_tweet_id == c.conversation_id
            )
            oon_reply_dropped = is_reply and not (
                self.enable_phoenix_oon_replies
                and c.is_phoenix_home_retrieval
                and ((not self.phoenix_oon_replies_root_only) or replies_to_root)
            )
            should_drop = (c.in_network is False and (is_retweet or oon_reply_dropped)) or (
                is_reply and len(c.ancestors) == 0
            )
            if should_drop:
                removed.append(c)
            else:
                kept.append(c)
        return FilterResult(kept=kept, removed=removed)


class SelfReplyChainFilter:
    """Exact port of home-mixer/filters/self_reply_chain_filter.rs (9106e9f)."""

    def __init__(self, viewer_id: int, followed_user_ids: Sequence[int]) -> None:
        self.allowed_users: set[int] = {int(uid) for uid in followed_user_ids}
        self.allowed_users.add(int(viewer_id))

    def _should_keep(self, candidate: FilterCandidate) -> bool:
        is_reply = candidate.in_reply_to_tweet_id is not None
        if (not is_reply) or len(candidate.ancestor_users) == 0 or candidate.author_id == 0:
            return True
        self_id = candidate.author_id
        for uid in candidate.ancestor_users:
            if uid != self_id:
                return uid in self.allowed_users
        return True

    def filter(self, candidates: Sequence[FilterCandidate]) -> FilterResult:
        kept: list[FilterCandidate] = []
        removed: list[FilterCandidate] = []
        for c in candidates:
            if self._should_keep(c):
                kept.append(c)
            else:
                removed.append(c)
        return FilterResult(kept=kept, removed=removed)


class AgeFilter:
    """Enforces thunder/ & SimclustersMaxCandidateAgeHours = 48h ceiling."""

    def __init__(self, max_age_hours: float = 48.0) -> None:
        self.max_age_secs = max_age_hours * 3600.0

    def filter(self, candidates: Sequence[FilterCandidate]) -> FilterResult:
        kept: list[FilterCandidate] = []
        removed: list[FilterCandidate] = []
        for c in candidates:
            if c.age_secs is not None and c.age_secs > self.max_age_secs:
                removed.append(c)
            else:
                kept.append(c)
        return FilterResult(kept=kept, removed=removed)


@dataclass(slots=True)
class ColdStartConfig:
    """Parameters from home-mixer/scorers/author_cold_start.rs & param.rs."""

    enabled: bool = True
    follower_cap: int = 50_000
    impression_threshold: int = 200
    max_post_age_secs: float = 7_200.0
    max_position_ratio: float = 0.97
    slot_min: int = 15
    slot_max: int = 16
    use_thompson_sampling: bool = True
    beta_alpha0: float = 0.75
    beta_beta0: float = 49.25
    impression_scale: float = 1.0
    ts_top_k: int = 2


@dataclass(slots=True)
class ColdStartDecision:
    scores: list[float]
    lifted_index: int | None = None
    target_rank: int | None = None


def is_cold_start_eligible(
    candidate: FilterCandidate,
    follower_cap: int = 50_000,
    max_post_age_secs: float = 7_200.0,
    impression_threshold: int = 200,
) -> bool:
    """Checks base + age + impression ColdStart eligibility per author_cold_start.rs."""
    base_ok = (
        candidate.in_reply_to_tweet_id is None
        and candidate.retweeted_tweet_id is None
        and candidate.author_followers_count is not None
        and candidate.author_followers_count <= follower_cap
    )
    if not base_ok:
        return False
    if candidate.age_secs is None or candidate.age_secs > max_post_age_secs:
        return False
    if (
        candidate.view_count_on_home is None
        or candidate.view_count_on_home >= impression_threshold
    ):
        return False
    return True


def sample_cold_start_reward(
    candidate: FilterCandidate,
    alpha0: float = 0.75,
    beta_0: float = 49.25,
    scale: float = 1.0,
    rng: np.random.Generator | None = None,
) -> float:
    """Exact port of sample_reward in author_cold_start.rs (Beta(0.75 + x, 49.25 + n - x))."""
    gen = rng if rng is not None else np.random.default_rng()
    n = scale * float(candidate.view_count_on_home or 0)
    x = min(float(max(candidate.fav_count or 0, 0)), n)
    alpha = alpha0 + x
    beta = beta_0 + max(0.0, n - x)
    if alpha <= 0.0 or beta <= 0.0:
        return 0.5
    return float(gen.beta(alpha, beta))


class AuthorColdStart:
    """Exact port of home-mixer/scorers/author_cold_start.rs Thompson Sampling rank lift."""

    def __init__(self, config: ColdStartConfig | None = None) -> None:
        self.config = config or ColdStartConfig()

    @staticmethod
    def _positions_among_nonzero(scores: Sequence[float]) -> tuple[list[int], int]:
        nonzero_indices = [i for i, s in enumerate(scores) if s != 0.0]
        nonzero_indices.sort(key=lambda idx: (-scores[idx], idx))
        positions = [10**9] * len(scores)
        for pos, idx in enumerate(nonzero_indices):
            positions[idx] = pos
        return positions, len(nonzero_indices)

    def apply(
        self,
        candidates: Sequence[FilterCandidate],
        scores: Sequence[float],
        rng: np.random.Generator | None = None,
    ) -> ColdStartDecision:
        cfg = self.config
        if not cfg.enabled or len(scores) == 0:
            return ColdStartDecision(scores=list(scores))

        gen = rng if rng is not None else np.random.default_rng()
        ranked = sorted(scores, reverse=True)
        hi = min(cfg.slot_max, len(ranked))
        lo = min(cfg.slot_min, hi)
        if lo >= hi:
            return ColdStartDecision(scores=list(scores))

        target_rank = int(gen.integers(lo, hi))
        target_score = ranked[target_rank]

        positions, nonzero = self._positions_among_nonzero(scores)
        max_cold_start_slot = int(cfg.max_position_ratio * float(nonzero))

        eligible = [
            i
            for i, c in enumerate(candidates)
            if is_cold_start_eligible(
                c,
                follower_cap=cfg.follower_cap,
                max_post_age_secs=cfg.max_post_age_secs,
                impression_threshold=cfg.impression_threshold,
            )
            and positions[i] < max_cold_start_slot
        ]
        if not eligible:
            return ColdStartDecision(scores=list(scores))

        if (
            cfg.use_thompson_sampling
            and cfg.ts_top_k > 0
            and cfg.beta_alpha0 > 0.0
            and cfg.beta_beta0 > 0.0
        ):
            sampled = [
                (
                    i,
                    sample_cold_start_reward(
                        candidates[i],
                        alpha0=cfg.beta_alpha0,
                        beta_0=cfg.beta_beta0,
                        scale=cfg.impression_scale,
                        rng=gen,
                    ),
                )
                for i in eligible
            ]
            sampled.sort(key=lambda pair: (-pair[1], pair[0]))
            top_slice = sampled[: min(cfg.ts_top_k, len(sampled))]
            best_idx = min((pair[0] for pair in top_slice), key=lambda idx: (-scores[idx], idx))
        else:
            best_idx = min(eligible, key=lambda idx: (-scores[idx], idx))

        effective = list(scores)
        effective[best_idx] = max(effective[best_idx], target_score)
        return ColdStartDecision(
            scores=effective,
            lifted_index=best_idx,
            target_rank=target_rank,
        )
