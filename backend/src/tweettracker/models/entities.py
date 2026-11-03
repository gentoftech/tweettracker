"""Authoritative Multi-Workspace SQLAlchemy 2.0 ORM Schema for TweetTracker Build 1."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """Base declarative class for all TweetTracker ORM models."""


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    slug: Mapped[str] = mapped_column(String(120), unique=True, index=True, nullable=False)
    my_x_handle: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    x_data_provider: Mapped[str] = mapped_column(String(32), nullable=False, default="fixture")
    llm_provider: Mapped[str] = mapped_column(String(32), nullable=False, default="google_gemini")
    radar_poll_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    use_x_lists: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    x_list_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    daily_spend_cap_usd: Mapped[float] = mapped_column(Float, nullable=False, default=2.0)
    monthly_spend_cap_usd: Mapped[float] = mapped_column(Float, nullable=False, default=25.0)
    monthly_unit_budget: Mapped[int] = mapped_column(Integer, nullable=False, default=100_000)
    # Hot Posts hides posts projected to stay under this many 48h views (too small to reply to)
    hot_posts_min_projected_views: Mapped[int] = mapped_column(Integer, nullable=False, default=5_000)
    watch_threshold: Mapped[float] = mapped_column(Float, nullable=False, default=65.0)
    surge_threshold: Mapped[float] = mapped_column(Float, nullable=False, default=82.0)
    quiet_hours_start: Mapped[str | None] = mapped_column(String(16), nullable=True, default=None)
    quiet_hours_end: Mapped[str | None] = mapped_column(String(16), nullable=True, default=None)
    max_alerts_per_hour: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    webhook_urls_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    last_discovery_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )

    communities: Mapped[list[Community]] = relationship(
        "Community",
        back_populates="workspace",
        cascade="all, delete-orphan",
        order_by="Community.sort_order",
    )
    tracked_accounts: Mapped[list[TrackedAccount]] = relationship(
        "TrackedAccount",
        back_populates="workspace",
        cascade="all, delete-orphan",
    )


class Community(Base):
    __tablename__ = "communities"
    __table_args__ = (
        UniqueConstraint("workspace_id", "slug", name="uq_communities_workspace_slug"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(120), nullable=False)
    color: Mapped[str] = mapped_column(String(16), nullable=False, default="#38bdf8")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    x_list_id: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    watch_threshold_override: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)
    surge_threshold_override: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    workspace: Mapped[Workspace] = relationship("Workspace", back_populates="communities")
    memberships: Mapped[list[AccountCommunityMembership]] = relationship(
        "AccountCommunityMembership",
        back_populates="community",
        cascade="all, delete-orphan",
    )


class TrackedAccount(Base):
    __tablename__ = "tracked_accounts"
    __table_args__ = (
        UniqueConstraint("workspace_id", "handle", name="uq_tracked_accounts_workspace_handle"),
        Index("ix_tracked_accounts_workspace_tracked", "workspace_id", "is_tracked"),
        Index("ix_tracked_accounts_workspace_hh", "workspace_id", "heavy_hitter_score"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    x_user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    handle: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    bio: Mapped[str] = mapped_column(Text, nullable=False, default="")
    avatar_url: Mapped[str | None] = mapped_column(String(512), nullable=True, default=None)
    followers_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    following_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    followers_7d_ago: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_tracked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_auto_tracked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_muted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    auto_tracked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
    follows_me: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    followed_by_me: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    relationship_state: Mapped[str] = mapped_column(String(32), nullable=False, default="none")
    last_interaction_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
    mutual_interaction_count_30d: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    heavy_hitter_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    catalysis_lift: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    s_core_depth: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    salsa_authority: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    salsa_scout: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    user_cred_v2: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    core_affinity_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    yield_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    reply_sample_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    avg_reply_views: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    attributed_follows_30d: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    bridge_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    participation_coeff: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    burt_brokerage: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    bridge_role: Mapped[str] = mapped_column(String(32), nullable=False, default="provincial")
    sub_pocket_label: Mapped[str | None] = mapped_column(String(120), nullable=True, default=None)
    baseline_curves_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    last_synced_since_id: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    last_follow_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)

    workspace: Mapped[Workspace] = relationship("Workspace", back_populates="tracked_accounts")
    memberships: Mapped[list[AccountCommunityMembership]] = relationship(
        "AccountCommunityMembership",
        back_populates="account",
        cascade="all, delete-orphan",
    )
    posts: Mapped[list[Post]] = relationship(
        "Post",
        back_populates="author",
        cascade="all, delete-orphan",
    )


class AccountCommunityMembership(Base):
    __tablename__ = "account_community_memberships"
    __table_args__ = (
        UniqueConstraint("account_id", "community_id", name="uq_membership_account_community"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tracked_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    community_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("communities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    is_manual_tag: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    nmf_known_for: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    nmf_interested_in: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    account: Mapped[TrackedAccount] = relationship("TrackedAccount", back_populates="memberships")
    community: Mapped[Community] = relationship("Community", back_populates="memberships")


class RelationshipEdge(Base):
    __tablename__ = "relationship_edges"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "source_account_id",
            "target_account_id",
            "edge_type",
            name="uq_relationship_edge_tuple",
        ),
        Index("ix_relationship_edges_workspace_source", "workspace_id", "source_account_id"),
        Index("ix_relationship_edges_workspace_target", "workspace_id", "target_account_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tracked_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tracked_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    edge_type: Mapped[str] = mapped_column(String(24), nullable=False)
    interaction_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    decayed_weight: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)


class Post(Base):
    __tablename__ = "posts"
    __table_args__ = (
        UniqueConstraint("workspace_id", "x_post_id", name="uq_posts_workspace_x_post_id"),
        Index("ix_posts_workspace_created", "workspace_id", "created_at_x"),
        Index("ix_posts_workspace_breakout", "workspace_id", "breakout_score"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    x_post_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    author_account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tracked_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at_x: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    is_reply: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_retweet: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_quote: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    in_reply_to_post_id: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    conversation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    quoted_post_id: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    has_url: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    has_media: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    media_type: Mapped[str | None] = mapped_column(String(32), nullable=True, default=None)
    video_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    ancestor_author_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    embedding_json: Mapped[list[float]] = mapped_column(JSON, nullable=False, default=list)
    latest_views: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latest_likes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latest_replies: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latest_quotes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latest_reposts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latest_bookmarks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    breakout_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, index=True)
    velocity_z_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    seismic_infectiousness: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    value_model_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    dpp_novelty_multiplier: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    catalysis_boost: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    recommended_action: Mapped[str] = mapped_column(String(32), nullable=False, default="watch")
    queue_status: Mapped[str] = mapped_column(String(24), nullable=False, default="open", index=True)
    acted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
    acted_action_kind: Mapped[str | None] = mapped_column(String(16), nullable=True, default=None)
    matured_48h: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    actual_48h_views: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    actual_48h_multiple: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)
    is_true_breakout_48h: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=None)

    author: Mapped[TrackedAccount] = relationship("TrackedAccount", back_populates="posts")
    snapshots: Mapped[list[PostSnapshot]] = relationship(
        "PostSnapshot",
        back_populates="post",
        cascade="all, delete-orphan",
        order_by="PostSnapshot.age_minutes",
    )
    interactions: Mapped[list[PostInteraction]] = relationship(
        "PostInteraction",
        back_populates="post",
        cascade="all, delete-orphan",
    )


class PostSnapshot(Base):
    __tablename__ = "post_snapshots"
    __table_args__ = (
        Index("ix_post_snapshots_post_age", "post_id", "age_minutes"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    post_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("posts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    age_minutes: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    views: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    likes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    replies: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    quotes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reposts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    bookmarks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    breakout_score_at_capture: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    post: Mapped[Post] = relationship("Post", back_populates="snapshots")


class PostInteraction(Base):
    __tablename__ = "post_interactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    post_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("posts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    actor_account_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("tracked_accounts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    actor_x_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_handle: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_followers: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    interaction_type: Mapped[str] = mapped_column(String(24), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)

    post: Mapped[Post] = relationship("Post", back_populates="interactions")


class AlgoParamVersion(Base):
    __tablename__ = "algo_param_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=True, index=True
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    label: Mapped[str] = mapped_column(String(160), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="upstream_xalgo")
    upstream_commit_sha: Mapped[str] = mapped_column(String(40), nullable=False, default="9106e9f")
    weights_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    surrogate_params_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    thresholds_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    holdout_metrics_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    changelog_note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class BreakoutPrediction(Base):
    __tablename__ = "breakout_predictions"
    __table_args__ = (
        Index("ix_breakout_predictions_workspace_post", "workspace_id", "post_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    post_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("posts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    param_version_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("algo_param_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    predicted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    post_age_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    breakout_probability: Mapped[float] = mapped_column(Float, nullable=False)
    breakout_score: Mapped[float] = mapped_column(Float, nullable=False)
    alert_tier: Mapped[str] = mapped_column(String(24), nullable=False, default="none")
    evaluated_at_48h: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
    brier_error: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)
    outcome_class: Mapped[str | None] = mapped_column(String(24), nullable=True, default=None)


class AlertDelivery(Base):
    __tablename__ = "alert_deliveries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    post_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("posts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    channel: Mapped[str] = mapped_column(String(24), nullable=False)
    target_url: Mapped[str | None] = mapped_column(String(512), nullable=True, default=None)
    alert_tier: Mapped[str] = mapped_column(String(24), nullable=False, default="none")
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="delivered")
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, index=True)


class DiscoveredCandidate(Base):
    __tablename__ = "discovered_candidates"
    __table_args__ = (
        UniqueConstraint("workspace_id", "handle", name="uq_discovered_candidates_workspace_handle"),
        Index("ix_discovered_candidates_workspace_score", "workspace_id", "combined_rising_score"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    x_user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    handle: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    bio: Mapped[str] = mapped_column(Text, nullable=False, default="")
    avatar_url: Mapped[str | None] = mapped_column(String(512), nullable=True, default=None)
    followers_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    following_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    followers_growth_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    sampled_posts_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    posts_above_3x_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    median_reach_multiple: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    punch_above_weight_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    peer_respect_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    combined_rising_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, index=True)
    core_affinity_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    yield_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    reply_sample_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    avg_reply_views: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    attributed_follows_30d: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    community_projection_json: Mapped[dict[str, float]] = mapped_column(JSON, nullable=False, default=dict)
    top_posts_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active")
    # Set when an auto-added account is pruned; it re-qualifies only on newer reply/quote evidence
    pruned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )

    endorsements: Mapped[list[PeerEndorsement]] = relationship(
        "PeerEndorsement",
        back_populates="candidate",
        cascade="all, delete-orphan",
    )


class PeerEndorsement(Base):
    __tablename__ = "peer_endorsements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    candidate_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("discovered_candidates.id", ondelete="CASCADE"), nullable=False, index=True
    )
    endorser_account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tracked_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    endorsement_type: Mapped[str] = mapped_column(String(24), nullable=False)
    source_x_post_id: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    weight_contribution: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)

    candidate: Mapped[DiscoveredCandidate] = relationship("DiscoveredCandidate", back_populates="endorsements")


class UpstreamAlgoEvent(Base):
    __tablename__ = "upstream_algo_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    base_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    head_sha: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    watched_files_changed_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    param_diffs_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    structural_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    github_issue_number: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    github_issue_url: Mapped[str | None] = mapped_column(String(512), nullable=True, default=None)
    staged_param_version_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("algo_param_versions.id", ondelete="SET NULL"), nullable=True, default=None
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="synced")


class ApiCreditLedger(Base):
    __tablename__ = "api_credit_ledger"
    __table_args__ = (
        Index("ix_api_credit_ledger_workspace_created", "workspace_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("workspaces.id", ondelete="SET NULL"), nullable=True, index=True
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    endpoint: Mapped[str] = mapped_column(String(160), nullable=False)
    units_consumed: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    estimated_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    reconciled_balance_units: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, index=True)


class AppSecret(Base):
    """Encrypted-at-rest API credentials managed from the Settings UI."""

    __tablename__ = "app_secrets"

    key_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    encrypted_value: Mapped[str] = mapped_column(Text, nullable=False)
    masked_hint: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )



class OwnFollowerSnapshot(Base):
    """Time-series follower snapshots for `workspace.my_x_handle` to attribute post-reply follow gains."""

    __tablename__ = "own_follower_snapshots"
    __table_args__ = (
        Index("ix_own_follower_snapshots_ws_recorded", "workspace_id", "recorded_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, index=True
    )
    followers_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class OwnReplyOutcome(Base):
    """Observed reach & follower attribution for replies/quotes sent by `workspace.my_x_handle`."""

    __tablename__ = "own_reply_outcomes"
    __table_args__ = (
        UniqueConstraint("workspace_id", "x_reply_post_id", name="uq_own_reply_outcomes_ws_post"),
        Index("ix_own_reply_outcomes_ws_target", "workspace_id", "target_account_id"),
        Index("ix_own_reply_outcomes_ws_handle", "workspace_id", "target_handle"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    x_reply_post_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    target_account_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("tracked_accounts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    target_handle: Mapped[str] = mapped_column(String(64), nullable=False, default="", index=True)
    source_post_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("posts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    target_x_post_id: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    action_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="reply")
    from_hot_posts: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at_x: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, index=True
    )
    post_age_at_reply_min: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    post_breakout_at_reply: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)
    was_mutual_at_reply: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    latest_views: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latest_likes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latest_replies: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latest_quotes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latest_bookmarks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attributed_follows_gained: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    matured_24h: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_refreshed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
