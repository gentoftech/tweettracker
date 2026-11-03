"""Exact port of xai-org/x-algorithm (9106e9f) ValueModelWeights and parameter defaults."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
import hashlib
import math
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tweettracker.algo.scoring import CandidateScoringInputs

NEGATIVE_SCORES_OFFSET: float = 0.001
UPSTREAM_COMMIT_SHA: str = "9106e9f"


def _coerce_finite_float(
    key: str,
    val: Any,
    *,
    min_val: float | None = None,
    max_val: float | None = None,
) -> float:
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise ValueError(f"Parameter '{key}' must be a finite number.")
    f_val = float(val)
    if not math.isfinite(f_val):
        raise ValueError(f"Parameter '{key}' must be finite (no NaN or Inf).")
    if min_val is not None and f_val < min_val:
        raise ValueError(f"Parameter '{key}' ({f_val}) must be >= {min_val}.")
    if max_val is not None and f_val > max_val:
        raise ValueError(f"Parameter '{key}' ({f_val}) must be <= {max_val}.")
    return f_val


def _coerce_int(
    key: str,
    val: Any,
    *,
    min_val: int | None = None,
    max_val: int | None = None,
) -> int:
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise ValueError(f"Parameter '{key}' must be an integer.")
    if isinstance(val, float):
        if not math.isfinite(val) or not val.is_integer():
            raise ValueError(f"Parameter '{key}' must be a finite integer.")
    i_val = int(val)
    if min_val is not None and i_val < min_val:
        raise ValueError(f"Parameter '{key}' ({i_val}) must be >= {min_val}.")
    if max_val is not None and i_val > max_val:
        raise ValueError(f"Parameter '{key}' ({i_val}) must be <= {max_val}.")
    return i_val


def _coerce_bool(key: str, val: Any) -> bool:
    if not isinstance(val, bool):
        raise ValueError(f"Parameter '{key}' must be a boolean.")
    return val


def perturbation_sign(salt: str, user_id: int, head: str) -> float:
    """Exact port of xai-value-model/weights.rs perturbation_sign (MD5 bit 0)."""
    digest = hashlib.md5(f"{salt}:{user_id}:{head}".encode("utf-8")).digest()
    return 1.0 if (digest[0] & 1) == 1 else -1.0


_VM_BOOL_FIELDS: frozenset[str] = frozenset(
    {
        "post_unexplored_include_out_of_network",
        "enable_quoted_vqv_duration_check",
        "enable_author_diversity",
        "oon_rescore_in_network_replies_retweets",
        "multiplier_pre_offset",
    }
)
_VM_INT_FIELDS: frozenset[str] = frozenset({"min_video_duration_ms"})


@dataclass(slots=True)
class ValueModelWeights:
    """Exact field-for-field port of xai-value-model/src/weights.rs (HEAD = 9106e9f)."""

    favorite: float = 0.0
    reply: float = 0.0
    retweet: float = 0.0
    photo_expand: float = 0.0
    video_open: float = 0.0
    click: float = 0.0
    open_link: float = 0.0
    profile_click: float = 0.0
    vqv: float = 0.0
    share: float = 0.0
    share_via_dm: float = 0.0
    share_via_copy_link: float = 0.0
    dwell: float = 0.0
    quote: float = 0.0
    quoted_click: float = 0.0
    quoted_vqv: float = 0.0
    follow_author: float = 0.0
    post_unexplored: float = 0.0
    post_unexplored_include_out_of_network: bool = False
    post_video: float = 0.0
    not_interested: float = 0.0
    block_author: float = 0.0
    mute_author: float = 0.0
    report: float = 0.0
    not_dwelled: float = 0.0
    cont_dwell_time: float = 0.0
    cont_click_dwell_time: float = 0.0
    video_continuation: float = 0.0
    user_video_continuation: float = 0.0
    profile_visit_secs: float = 0.0
    min_video_duration_ms: int = 0
    enable_quoted_vqv_duration_check: bool = False
    bidirectional_follow_reply_weight_boost: float = 0.0
    bidirectional_follow_dwell_weight_boost: float = 0.0
    enable_author_diversity: bool = False
    author_diversity_decay: float = 0.0
    author_diversity_floor: float = 0.0
    oon_rescore_in_network_replies_retweets: bool = False
    multiplier_pre_offset: bool = False

    def positive_sum(self) -> float:
        """Exact sum of the 19 discrete positive weights per weights.rs lines 50-70."""
        terms = (
            self.favorite,
            self.reply,
            self.retweet,
            self.photo_expand,
            self.video_open,
            self.click,
            self.open_link,
            self.profile_click,
            self.vqv,
            self.share,
            self.share_via_dm,
            self.share_via_copy_link,
            self.dwell,
            self.quote,
            self.quoted_click,
            self.quoted_vqv,
            self.follow_author,
            self.post_unexplored,
            self.post_video,
        )
        total = 0.0
        for term in terms:
            total += term
        return total

    def negative_sum(self) -> float:
        """Exact magnitude of the 5 negative feedback weights per weights.rs lines 72-78."""
        return -(
            self.not_interested
            + self.block_author
            + self.mute_author
            + self.report
            + self.not_dwelled
        )

    def total_sum(self) -> float:
        return self.positive_sum() + self.negative_sum()

    def reply_weight_for(self, candidate: CandidateScoringInputs) -> float:
        if (
            self.bidirectional_follow_reply_weight_boost != 0.0
            and candidate.bidirectional_boost_eligible()
        ):
            return self.reply + self.bidirectional_follow_reply_weight_boost
        return self.reply

    def dwell_weight_for(self, candidate: CandidateScoringInputs) -> float:
        if (
            self.bidirectional_follow_dwell_weight_boost != 0.0
            and candidate.bidirectional_boost_eligible()
        ):
            return self.dwell + self.bidirectional_follow_dwell_weight_boost
        return self.dwell

    def perturbed(self, sigma: float, salt: str, user_id: int) -> ValueModelWeights:
        """Exact port of ValueModelWeights::perturbed in weights.rs lines 102-144."""
        cloned = replace(self)
        if sigma <= 0.0:
            return cloned
        head_attr_pairs = (
            ("favorite", "favorite"),
            ("reply", "reply"),
            ("retweet", "retweet"),
            ("photo_expand", "photo_expand"),
            ("video_open", "video_open"),
            ("click", "click"),
            ("open_link", "open_link"),
            ("profile_click", "profile_click"),
            ("vqv", "vqv"),
            ("share", "share"),
            ("share_via_dm", "share_via_dm"),
            ("share_via_copy_link", "share_via_copy_link"),
            ("dwell", "dwell"),
            ("quote", "quote"),
            ("quoted_click", "quoted_click"),
            ("quoted_vqv", "quoted_vqv"),
            ("dwell_time", "cont_dwell_time"),
            ("click_dwell_time", "cont_click_dwell_time"),
            ("video_continuation", "video_continuation"),
            ("user_video_continuation", "user_video_continuation"),
            ("profile_visit_secs", "profile_visit_secs"),
            ("follow_author", "follow_author"),
            ("post_unexplored", "post_unexplored"),
            ("post_video", "post_video"),
            ("not_interested", "not_interested"),
            ("block_author", "block_author"),
            ("mute_author", "mute_author"),
            ("report", "report"),
            ("not_dwelled", "not_dwelled"),
        )
        for head, attr in head_attr_pairs:
            val = getattr(cloned, attr)
            setattr(cloned, attr, val * math.exp(sigma * perturbation_sign(salt, user_id, head)))
        return cloned

    def applied_weights_map(self) -> dict[str, float]:
        """Exact port of ValueModelWeights::applied_weights_map in weights.rs lines 146-195."""
        return {
            "favorite": self.favorite,
            "reply": self.reply,
            "retweet": self.retweet,
            "photo_expand": self.photo_expand,
            "video_open": self.video_open,
            "click": self.click,
            "open_link": self.open_link,
            "profile_click": self.profile_click,
            "vqv": self.vqv,
            "share": self.share,
            "share_via_dm": self.share_via_dm,
            "share_via_copy_link": self.share_via_copy_link,
            "dwell": self.dwell,
            "quote": self.quote,
            "quoted_click": self.quoted_click,
            "quoted_vqv": self.quoted_vqv,
            "follow_author": self.follow_author,
            "post_unexplored": self.post_unexplored,
            "pdwell": self.post_unexplored,
            "post_video": self.post_video,
            "not_interested": self.not_interested,
            "block_author": self.block_author,
            "mute_author": self.mute_author,
            "report": self.report,
            "not_dwelled": self.not_dwelled,
            "dwell_time": self.cont_dwell_time,
            "click_dwell_time": self.cont_click_dwell_time,
            "boost.bidirectional_follow_reply": self.bidirectional_follow_reply_weight_boost,
            "boost.bidirectional_follow_dwell": self.bidirectional_follow_dwell_weight_boost,
            "gate.quoted_vqv_duration_check": float(1 if self.enable_quoted_vqv_duration_check else 0),
            "gate.min_video_duration_ms": float(self.min_video_duration_ms),
        }

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> ValueModelWeights:
        valid: dict[str, Any] = {}
        for k, v in raw.items():
            if k not in cls.__dataclass_fields__ or v is None:
                continue
            if k in _VM_BOOL_FIELDS:
                valid[k] = _coerce_bool(k, v)
            elif k in _VM_INT_FIELDS:
                valid[k] = _coerce_int(k, v, min_val=0, max_val=3_600_000)
            elif k in ("author_diversity_decay", "author_diversity_floor"):
                valid[k] = _coerce_finite_float(k, v, min_val=0.0, max_val=1.0)
            else:
                valid[k] = _coerce_finite_float(k, v, min_val=-10_000.0, max_val=10_000.0)
        return cls(**valid)

    @classmethod
    def vm_ranker_defaults(cls) -> ValueModelWeights:
        """Exact live defaults from vm-ranker/src/params.rs at 9106e9f (2026-10-31)."""
        return cls(
            favorite=0.5,
            reply=5.0,
            retweet=1.0,
            photo_expand=0.05,
            video_open=0.07,
            click=0.3,
            open_link=0.2,
            profile_click=0.0,
            vqv=0.0,
            share=2.0,
            share_via_dm=5.0,
            share_via_copy_link=20.0,
            dwell=0.05,
            quote=5.0,
            quoted_click=0.05,
            quoted_vqv=0.0,
            cont_dwell_time=0.004,
            cont_click_dwell_time=0.4,
            video_continuation=0.0,
            user_video_continuation=0.0,
            profile_visit_secs=0.0,
            follow_author=4.0,
            post_unexplored=0.02,
            post_unexplored_include_out_of_network=False,
            post_video=0.0,
            not_interested=-47.52,
            block_author=-31.2,
            mute_author=-58.8,
            report=-234.0,
            not_dwelled=-0.02,
            min_video_duration_ms=10_000,
            enable_quoted_vqv_duration_check=False,
            bidirectional_follow_reply_weight_boost=15.0,
            bidirectional_follow_dwell_weight_boost=0.0,
            enable_author_diversity=True,
            author_diversity_decay=0.5,
            author_diversity_floor=0.25,
            oon_rescore_in_network_replies_retweets=True,
            multiplier_pre_offset=False,
        )

    @classmethod
    def home_mixer_defaults(cls) -> ValueModelWeights:
        """Exact live defaults from home-mixer/params/param.rs at 9106e9f (2026-10-31)."""
        return cls(
            favorite=0.66,
            reply=5.0,
            retweet=1.0,
            photo_expand=0.05,
            video_open=0.02,
            click=0.3,
            open_link=0.2,
            profile_click=0.0,
            vqv=0.0,
            share=2.0,
            share_via_dm=5.0,
            share_via_copy_link=20.0,
            dwell=0.05,
            quote=5.0,
            quoted_click=0.05,
            quoted_vqv=0.0,
            cont_dwell_time=0.006,
            cont_click_dwell_time=0.4,
            video_continuation=0.0,
            user_video_continuation=0.0,
            profile_visit_secs=0.0,
            follow_author=4.0,
            post_unexplored=0.015,
            post_unexplored_include_out_of_network=False,
            post_video=0.0,
            not_interested=-52.272,
            block_author=-31.2,
            mute_author=-58.8,
            report=-234.0,
            not_dwelled=-0.02,
            min_video_duration_ms=10_000,
            enable_quoted_vqv_duration_check=False,
            bidirectional_follow_reply_weight_boost=15.0,
            bidirectional_follow_dwell_weight_boost=0.0,
            enable_author_diversity=True,
            author_diversity_decay=0.5,
            author_diversity_floor=0.25,
            oon_rescore_in_network_replies_retweets=True,
            multiplier_pre_offset=False,
        )


_PIPE_BOOL_FIELDS: frozenset[str] = frozenset(
    {
        "dpp_enabled",
        "topic_dpp_enabled",
        "enable_phoenix_oon_replies",
        "phoenix_oon_replies_root_only",
        "enable_viewer_cold_start",
        "enable_cold_start_thompson_sampling",
    }
)
_PIPE_INT_FIELDS: frozenset[str] = frozenset(
    {
        "dpp_max_selected_rank",
        "simclusters_max_candidate_age_hours",
        "cold_start_follower_cap",
        "cold_start_max_post_age_secs",
        "cold_start_impression_threshold",
        "cold_start_slot_min",
        "cold_start_slot_max",
        "cold_start_ts_top_k",
    }
)


@dataclass(slots=True)
class PipelineParams:
    """Pipeline-level constants from vm-ranker/params.rs & home-mixer/params/param.rs."""

    oon_weight_factor: float = 0.75
    topic_oon_weight_factor: float = 0.5
    new_user_oon_weight_factor: float = 0.00001
    dpp_enabled: bool = True
    topic_dpp_enabled: bool = False
    dpp_theta: float = 0.65
    dpp_max_selected_rank: int = 150
    simclusters_max_candidate_age_hours: int = 48
    enable_phoenix_oon_replies: bool = False
    phoenix_oon_replies_root_only: bool = False
    cold_start_follower_cap: int = 50_000
    cold_start_max_post_age_secs: int = 7_200
    cold_start_impression_threshold: int = 200
    cold_start_slot_min: int = 15
    cold_start_slot_max: int = 16
    low_impressions_max_position_ratio: float = 0.97
    enable_viewer_cold_start: bool = True
    enable_cold_start_thompson_sampling: bool = True
    cold_start_beta_alpha0: float = 0.75
    cold_start_beta_beta0: float = 49.25
    cold_start_ts_top_k: int = 2
    cold_start_impression_scale: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> PipelineParams:
        if not raw:
            return cls()
        valid: dict[str, Any] = {}
        for k, v in raw.items():
            if k not in cls.__dataclass_fields__ or v is None:
                continue
            if k in _PIPE_BOOL_FIELDS:
                valid[k] = _coerce_bool(k, v)
            elif k in _PIPE_INT_FIELDS:
                valid[k] = _coerce_int(k, v, min_val=0, max_val=100_000_000)
            elif k in ("dpp_theta", "low_impressions_max_position_ratio"):
                valid[k] = _coerce_finite_float(k, v, min_val=0.0, max_val=1.0)
            elif k in (
                "cold_start_beta_alpha0",
                "cold_start_beta_beta0",
                "cold_start_impression_scale",
            ):
                valid[k] = _coerce_finite_float(k, v, min_val=1e-6, max_val=10_000.0)
            else:
                valid[k] = _coerce_finite_float(k, v, min_val=0.0, max_val=10.0)
        return cls(**valid)


@dataclass(slots=True)
class FusionCalibrationParams:
    """Unified Stage 1 + Stage 2 breakout fusion and logistic calibration coefficients."""

    intercept: float = 36.0
    vel_weight: float = 12.8
    seismic_weight: float = 22.0
    vm_weight: float = 8.5
    vm_cap: float = 3.0
    catalysis_weight: float = 14.0
    cold_start_bonus: float = 2.5
    dpp_blend: float = 0.48
    prob_midpoint: float = 65.0
    prob_scale: float = 9.5

    def to_dict(self) -> dict[str, float]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> FusionCalibrationParams:
        if not raw:
            return cls()
        valid: dict[str, float] = {}
        for k, v in raw.items():
            if k not in cls.__dataclass_fields__ or v is None:
                continue
            if k == "dpp_blend":
                valid[k] = _coerce_finite_float(k, v, min_val=0.0, max_val=1.0)
            elif k == "prob_scale":
                valid[k] = _coerce_finite_float(k, v, min_val=0.5, max_val=100.0)
            elif k == "vm_cap":
                valid[k] = _coerce_finite_float(k, v, min_val=0.0, max_val=100.0)
            else:
                valid[k] = _coerce_finite_float(k, v, min_val=-500.0, max_val=500.0)
        return cls(**valid)
