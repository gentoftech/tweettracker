"""Pydantic v2 request and response schemas for TweetTracker API."""

from __future__ import annotations

from datetime import datetime
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tweettracker.providers.base import VALID_LLM_PROVIDERS, VALID_X_DATA_PROVIDERS
from tweettracker.radar.webhooks import (
    MAX_WEBHOOK_URLS_PER_WORKSPACE,
    validate_outbound_webhook_url,
)

_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
_X_LIST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_X_LIST_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:x\.com|twitter\.com)/i/lists/([A-Za-z0-9_-]{1,64})(?:/|\?|$)",
    re.IGNORECASE,
)
_HHMM_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def _validate_x_list_id_value(v: str | None) -> str | None:
    if v is None:
        return None
    s = v.strip()
    if not s:
        return None
    url_match = _X_LIST_URL_RE.search(s)
    if url_match:
        s = url_match.group(1)
    if not _X_LIST_ID_RE.match(s):
        raise ValueError("x_list_id must match ^[A-Za-z0-9_-]{1,64}$ or an x.com/i/lists/<id> URL")
    return s


def _validate_x_list_ids_list(raw_ids: list[str]) -> list[str]:
    cleaned: list[str] = []
    for raw in raw_ids:
        if not isinstance(raw, str):
            raise ValueError("Each X List ID must be a string.")
        for part in re.split(r"[\s,]+", raw.strip()):
            if not part:
                continue
            lid = _validate_x_list_id_value(part)
            if lid and lid not in cleaned:
                cleaned.append(lid)
    if len(cleaned) > 25:
        raise ValueError("At most 25 X List IDs are allowed per workspace.")
    return cleaned


def _validate_hhmm_value(v: str | None, field_name: str) -> str | None:
    if v is None:
        return None
    s = v.strip()
    if not s:
        return None
    if not _HHMM_RE.match(s):
        raise ValueError(f"{field_name} must be strict HH:MM (00:00–23:59)")
    return s


def _validate_webhook_urls_list(urls: list[str]) -> list[str]:
    cleaned: list[str] = []
    for raw in urls:
        if not isinstance(raw, str):
            raise ValueError("Each webhook URL must be a string.")
        s = raw.strip()
        if not s:
            continue
        validated = validate_outbound_webhook_url(s, resolve_dns=False)
        if validated not in cleaned:
            cleaned.append(validated)
    if len(cleaned) > MAX_WEBHOOK_URLS_PER_WORKSPACE:
        raise ValueError(
            f"At most {MAX_WEBHOOK_URLS_PER_WORKSPACE} webhook URLs are allowed per workspace."
        )
    return cleaned


def slugify(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return cleaned or "item"


class AuthVerifyRequest(BaseModel):
    password: str = ""


class CommunityCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    slug: str | None = Field(default=None, max_length=120)
    color: str = Field(default="#38bdf8")
    description: str = Field(default="")
    x_list_id: str | None = Field(default=None, max_length=64)
    watch_threshold_override: float | None = Field(default=None, ge=0.0, le=100.0, allow_inf_nan=False)
    surge_threshold_override: float | None = Field(default=None, ge=0.0, le=100.0, allow_inf_nan=False)
    sort_order: int | None = None

    @field_validator("color")
    @classmethod
    def _validate_color(cls, v: str) -> str:
        val = v.strip()
        if not _HEX_COLOR_RE.match(val):
            raise ValueError("color must be a 6-digit hex code like #38bdf8")
        return val.lower()

    @field_validator("x_list_id")
    @classmethod
    def _clean_list_id(cls, v: str | None) -> str | None:
        return _validate_x_list_id_value(v)

    @model_validator(mode="after")
    def _check_threshold_pair(self) -> CommunityCreate:
        if (
            self.watch_threshold_override is not None
            and self.surge_threshold_override is not None
            and self.watch_threshold_override >= self.surge_threshold_override
        ):
            raise ValueError("watch_threshold_override must be strictly less than surge_threshold_override")
        return self


class CommunityUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    color: str | None = None
    description: str | None = None
    x_list_id: str | None = None
    watch_threshold_override: float | None = Field(default=None, ge=0.0, le=100.0, allow_inf_nan=False)
    surge_threshold_override: float | None = Field(default=None, ge=0.0, le=100.0, allow_inf_nan=False)
    sort_order: int | None = None

    @field_validator("color")
    @classmethod
    def _validate_color(cls, v: str | None) -> str | None:
        if v is None:
            return None
        val = v.strip()
        if not _HEX_COLOR_RE.match(val):
            raise ValueError("color must be a 6-digit hex code like #38bdf8")
        return val.lower()

    @field_validator("x_list_id")
    @classmethod
    def _clean_list_id(cls, v: str | None) -> str | None:
        return _validate_x_list_id_value(v)

    @model_validator(mode="after")
    def _check_threshold_pair(self) -> CommunityUpdate:
        if (
            self.watch_threshold_override is not None
            and self.surge_threshold_override is not None
            and self.watch_threshold_override >= self.surge_threshold_override
        ):
            raise ValueError("watch_threshold_override must be strictly less than surge_threshold_override")
        return self


class CommunityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    workspace_id: int
    name: str
    slug: str
    color: str
    description: str
    x_list_id: str | None
    watch_threshold_override: float | None
    surge_threshold_override: float | None
    sort_order: int
    accounts_count: int = 0


def _normalize_handle(v: str | None) -> str | None:
    if v is None:
        return None
    cleaned = v.strip().lstrip("@").strip()
    if not cleaned:
        return None
    if not _HANDLE_RE.match(cleaned):
        raise ValueError("my_x_handle must match ^[A-Za-z0-9_]{1,15}$")
    return cleaned


class WorkspaceCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=160)
    slug: str | None = Field(default=None, max_length=120)
    my_x_handle: str | None = Field(default=None, max_length=64)
    x_data_provider: str = Field(default="fixture")
    llm_provider: str = Field(default="google_gemini")
    radar_poll_minutes: int = Field(default=10, ge=2, le=1440)
    use_x_lists: bool = False
    x_list_ids_json: list[str] = Field(default_factory=list)
    monthly_unit_budget: int = Field(default=100_000, ge=1000, le=10_000_000)
    hot_posts_min_projected_views: int = Field(default=5_000, ge=0, le=10_000_000)
    daily_spend_cap_usd: float = Field(default=2.0, ge=0.0, le=10_000.0, allow_inf_nan=False)
    monthly_spend_cap_usd: float = Field(default=25.0, ge=0.0, le=100_000.0, allow_inf_nan=False)
    watch_threshold: float = Field(default=65.0, ge=1.0, le=99.0, allow_inf_nan=False)
    surge_threshold: float = Field(default=82.0, ge=1.0, le=100.0, allow_inf_nan=False)
    quiet_hours_start: str | None = None
    quiet_hours_end: str | None = None
    max_alerts_per_hour: int = Field(default=10, ge=1, le=500)
    webhook_urls_json: list[str] = Field(default_factory=list)
    initial_communities: list[CommunityCreate] | None = None

    @field_validator("my_x_handle")
    @classmethod
    def _clean_my_handle(cls, v: str | None) -> str | None:
        return _normalize_handle(v)

    @field_validator("x_data_provider")
    @classmethod
    def _check_x_provider(cls, v: str) -> str:
        val = v.strip().lower()
        if val not in VALID_X_DATA_PROVIDERS:
            raise ValueError(f"x_data_provider must be one of {VALID_X_DATA_PROVIDERS}")
        return val

    @field_validator("llm_provider")
    @classmethod
    def _check_llm_provider(cls, v: str) -> str:
        val = v.strip().lower()
        if val not in VALID_LLM_PROVIDERS:
            raise ValueError(f"llm_provider must be one of {VALID_LLM_PROVIDERS}")
        return val

    @field_validator("quiet_hours_start")
    @classmethod
    def _check_quiet_start(cls, v: str | None) -> str | None:
        return _validate_hhmm_value(v, "quiet_hours_start")

    @field_validator("quiet_hours_end")
    @classmethod
    def _check_quiet_end(cls, v: str | None) -> str | None:
        return _validate_hhmm_value(v, "quiet_hours_end")

    @field_validator("webhook_urls_json")
    @classmethod
    def _check_webhooks(cls, v: list[str]) -> list[str]:
        return _validate_webhook_urls_list(v)

    @field_validator("x_list_ids_json")
    @classmethod
    def _check_x_list_ids(cls, v: list[str]) -> list[str]:
        return _validate_x_list_ids_list(v)

    @field_validator("initial_communities")
    @classmethod
    def _check_initial_communities(
        cls, v: list[CommunityCreate] | None
    ) -> list[CommunityCreate] | None:
        if v is not None and len(v) < 1:
            raise ValueError("initial_communities must contain at least 1 community when provided.")
        return v

    @model_validator(mode="after")
    def _check_thresholds(self) -> WorkspaceCreate:
        if self.watch_threshold >= self.surge_threshold:
            raise ValueError("watch_threshold must be strictly less than surge_threshold")
        return self


class WorkspaceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    my_x_handle: str | None = None
    x_data_provider: str | None = None
    llm_provider: str | None = None
    radar_poll_minutes: int | None = Field(default=None, ge=2, le=1440)
    use_x_lists: bool | None = None
    x_list_ids_json: list[str] | None = None
    monthly_unit_budget: int | None = Field(default=None, ge=1000, le=10_000_000)
    hot_posts_min_projected_views: int | None = Field(default=None, ge=0, le=10_000_000)
    daily_spend_cap_usd: float | None = Field(default=None, ge=0.0, le=10_000.0, allow_inf_nan=False)
    monthly_spend_cap_usd: float | None = Field(default=None, ge=0.0, le=100_000.0, allow_inf_nan=False)
    watch_threshold: float | None = Field(default=None, ge=1.0, le=99.0, allow_inf_nan=False)
    surge_threshold: float | None = Field(default=None, ge=1.0, le=100.0, allow_inf_nan=False)
    quiet_hours_start: str | None = None
    quiet_hours_end: str | None = None
    max_alerts_per_hour: int | None = Field(default=None, ge=1, le=500)
    webhook_urls_json: list[str] | None = None

    @field_validator("my_x_handle")
    @classmethod
    def _clean_my_handle(cls, v: str | None) -> str | None:
        return _normalize_handle(v)

    @field_validator("x_data_provider")
    @classmethod
    def _check_x_provider(cls, v: str | None) -> str | None:
        if v is None:
            return None
        val = v.strip().lower()
        if val not in VALID_X_DATA_PROVIDERS:
            raise ValueError(f"x_data_provider must be one of {VALID_X_DATA_PROVIDERS}")
        return val

    @field_validator("llm_provider")
    @classmethod
    def _check_llm_provider(cls, v: str | None) -> str | None:
        if v is None:
            return None
        val = v.strip().lower()
        if val not in VALID_LLM_PROVIDERS:
            raise ValueError(f"llm_provider must be one of {VALID_LLM_PROVIDERS}")
        return val

    @field_validator("quiet_hours_start")
    @classmethod
    def _check_quiet_start(cls, v: str | None) -> str | None:
        return _validate_hhmm_value(v, "quiet_hours_start")

    @field_validator("quiet_hours_end")
    @classmethod
    def _check_quiet_end(cls, v: str | None) -> str | None:
        return _validate_hhmm_value(v, "quiet_hours_end")

    @field_validator("webhook_urls_json")
    @classmethod
    def _check_webhooks(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        return _validate_webhook_urls_list(v)

    @field_validator("x_list_ids_json")
    @classmethod
    def _check_x_list_ids(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        return _validate_x_list_ids_list(v)

    @model_validator(mode="after")
    def _check_thresholds(self) -> WorkspaceUpdate:
        if (
            self.watch_threshold is not None
            and self.surge_threshold is not None
            and self.watch_threshold >= self.surge_threshold
        ):
            raise ValueError("watch_threshold must be strictly less than surge_threshold")
        return self


class WorkspaceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    slug: str
    my_x_handle: str | None
    x_data_provider: str
    llm_provider: str
    radar_poll_minutes: int
    use_x_lists: bool
    x_list_ids_json: list[str] = Field(default_factory=list)
    monthly_unit_budget: int = 100_000
    hot_posts_min_projected_views: int = 5_000
    daily_spend_cap_usd: float
    monthly_spend_cap_usd: float
    watch_threshold: float
    surge_threshold: float
    quiet_hours_start: str | None
    quiet_hours_end: str | None
    max_alerts_per_hour: int
    webhook_urls_json: list[str]
    created_at: datetime
    updated_at: datetime
    communities: list[CommunityOut] = Field(default_factory=list)
    tracked_accounts_count: int = 0
    posts_count: int = 0


class WorkspaceOverviewOut(BaseModel):
    workspace_id: int
    communities_count: int
    tracked_accounts_count: int
    mutual_accounts_count: int
    bridge_accounts_count: int
    active_posts_count: int
    matured_posts_count: int
    rising_candidates_count: int
    top_live_posts: list[dict[str, Any]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Epic 1: Community Network Graph, Heavy Hitters, Bridges & Mutual CRM Schemas
# ---------------------------------------------------------------------------


class CommunitySplitOut(BaseModel):
    community_id: int
    community_name: str
    community_slug: str
    color: str
    is_manual_tag: bool
    nmf_known_for: float
    nmf_interested_in: float
    pct: float


class NetworkAccountOut(BaseModel):
    id: int
    workspace_id: int
    x_user_id: str
    handle: str
    display_name: str
    bio: str
    avatar_url: str | None
    followers_count: int
    following_count: int
    followers_7d_ago: int
    follows_me: bool
    followed_by_me: bool
    relationship_state: str
    is_mutual_20x: bool
    mutual_interaction_count_30d: int
    last_interaction_at: datetime | None
    heavy_hitter_score: float
    community_heavy_hitter_score: float
    catalysis_lift: float
    catalyzed_posts_count: int
    s_core_depth: float
    salsa_authority: float
    salsa_scout: float
    user_cred_v2: float
    follower_efficiency: float
    bridge_score: float
    participation_coeff: float
    burt_brokerage: float
    bridge_role: str
    sub_pocket_label: str | None
    primary_community_id: int | None
    primary_community_name: str | None
    primary_community_color: str
    is_auto_tracked: bool = False
    is_muted: bool = False
    core_affinity_score: float = 0.0
    yield_score: float = 0.0
    reply_sample_count: int = 0
    avg_reply_views: float = 0.0
    attributed_follows_30d: float = 0.0
    influence_score: float = 0.0
    status_badge: str | None = None
    x_profile_url: str
    community_splits: list[CommunitySplitOut] = Field(default_factory=list)


class AccountImportRequest(BaseModel):
    handles_text: str | None = None
    csv_text: str | None = None
    handles: list[str] = Field(default_factory=list)
    community_ids: list[int] = Field(default_factory=list)
    x_list_id: str | None = None
    recompute: bool = True

    @field_validator("x_list_id")
    @classmethod
    def _clean_list_id(cls, v: str | None) -> str | None:
        return _validate_x_list_id_value(v)


class AccountImportResultOut(BaseModel):
    workspace_id: int
    imported_count: int
    updated_existing_count: int
    deduplicated_count: int
    total_tracked_accounts: int
    compute_duration_ms: float
    accounts: list[NetworkAccountOut] = Field(default_factory=list)


class AccountUpdateRequest(BaseModel):
    community_ids: list[int] | None = None
    followed_by_me: bool | None = None
    follows_me: bool | None = None
    is_muted: bool | None = None
    is_tracked: bool | None = None
    display_name: str | None = Field(default=None, max_length=160)
    bio: str | None = None


class AutoSyncNetworkOut(BaseModel):
    workspace_id: int
    auto_added_count: int
    auto_pruned_count: int
    active_tracked_count: int
    profiles_refreshed: int = 0
    own_replies_synced: int = 0
    projected_monthly_units: float = 0.0
    monthly_unit_budget: int = 100_000
    budget_throttle_mode: str = "normal"
    add_threshold_used: float = 0.55
    prune_threshold_used: float = 0.32
    auto_added_handles: list[str] = Field(default_factory=list)
    auto_pruned_handles: list[str] = Field(default_factory=list)


class RecomputeOut(BaseModel):
    workspace_id: int
    communities_count: int
    accounts_count: int
    edges_count: int
    bridges_count: int
    mutual_active_count: int
    compute_duration_ms: float


class XYCoordOut(BaseModel):
    x: float
    y: float


class GraphPoleOut(BaseModel):
    community_id: int
    name: str
    slug: str
    color: str
    x: float
    y: float
    accounts_count: int = 0


class GraphNodeOut(BaseModel):
    id: int
    handle: str
    display_name: str
    followers_count: int
    single_community_xy: XYCoordOut
    n_pole_xy: XYCoordOut
    color: str
    primary_community_id: int | None
    primary_community_name: str | None
    heavy_hitter_score: float
    community_heavy_hitter_score: float
    catalysis_lift: float
    catalyzed_posts_count: int
    s_core_depth: float
    salsa_authority: float
    salsa_scout: float
    user_cred_v2: float
    follower_efficiency: float
    bridge_score: float
    participation_coeff: float
    burt_brokerage: float
    bridge_role: str
    relationship_state: str
    is_mutual_20x: bool
    follows_me: bool
    followed_by_me: bool
    is_auto_tracked: bool = False
    is_muted: bool = False
    core_affinity_score: float = 0.0
    yield_score: float = 0.0
    reply_sample_count: int = 0
    avg_reply_views: float = 0.0
    attributed_follows_30d: float = 0.0
    influence_score: float = 0.0
    status_badge: str | None = None
    sub_pocket_label: str | None
    community_mix: list[CommunitySplitOut] = Field(default_factory=list)


class GraphEdgeOut(BaseModel):
    id: str
    source: int
    target: int
    weight: float
    edge_type: str
    is_mutual: bool
    cross_community: bool


class NetworkGraphOut(BaseModel):
    workspace_id: int
    mode: str
    k_communities: int
    bridge_map_available: bool
    has_own_handle: bool
    my_x_handle: str | None
    selected_community_id: int | None
    poles: list[GraphPoleOut] = Field(default_factory=list)
    nodes: list[GraphNodeOut] = Field(default_factory=list)
    edges: list[GraphEdgeOut] = Field(default_factory=list)


class CatalyzedConversationOut(BaseModel):
    post_id: int
    x_post_id: str
    author_handle: str
    author_display_name: str
    text: str
    interaction_type: str
    occurred_at: datetime | None
    latest_views: int
    observed_lift: float
    breakout_score: float
    x_post_url: str


class AccountPostSummaryOut(BaseModel):
    post_id: int
    x_post_id: str
    text: str
    created_at_x: datetime
    latest_views: int
    latest_likes: int
    latest_replies: int
    latest_quotes: int
    value_model_score: float
    breakout_score: float
    is_quote: bool
    x_post_url: str


class CrossCommunityThreadOut(BaseModel):
    peer_account_id: int
    peer_handle: str
    peer_display_name: str
    peer_community_name: str
    peer_community_color: str
    edge_type: str
    decayed_weight: float
    direction: str


class AccountDetailOut(BaseModel):
    account: NetworkAccountOut
    catalyzed_conversations: list[CatalyzedConversationOut] = Field(default_factory=list)
    recent_posts: list[AccountPostSummaryOut] = Field(default_factory=list)
    cross_community_threads: list[CrossCommunityThreadOut] = Field(default_factory=list)
    mutual_history: dict[str, Any] = Field(default_factory=dict)


class RelationshipRoiItemOut(BaseModel):
    account: NetworkAccountOut
    roi_score: float
    reciprocation_readiness: float
    recommended_play: str
    unlocks_20x_on_follow_back: bool
    x_profile_url: str


class RelationshipsOverviewOut(BaseModel):
    workspace_id: int
    has_own_handle: bool
    my_x_handle: str | None
    bidirectional_reply_weight: float = 20.0
    counts: dict[str, int] = Field(default_factory=dict)
    roi_queue: list[RelationshipRoiItemOut] = Field(default_factory=list)
    accounts: list[NetworkAccountOut] = Field(default_factory=list)


class SecretStatusOut(BaseModel):
    key_name: str
    label: str
    description: str
    configured: bool
    source: str
    masked_hint: str = ""
    has_env_fallback: bool = False
    updated_at: datetime | None = None


class SecretsListOut(BaseModel):
    secrets: list[SecretStatusOut] = Field(default_factory=list)


class SecretsUpdateRequest(BaseModel):
    secrets: dict[str, str | None] = Field(default_factory=dict)
