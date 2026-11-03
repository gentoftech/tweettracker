export type AlertTier = "surge" | "watch" | "none" | "test";
export type RecommendedAction = "reply_mutual_20x" | "quote_oon_safe" | "watch";
export type QueueStatus = "open" | "acted" | "skipped" | "saved";
export type ActionKind = "reply" | "quote";

export interface RadarHoverSummary {
  pace_label: string;
  typical_views: number;
  why_reasons: string[];
}

export interface RadarCommunityBadge {
  id: number;
  name: string;
  slug: string;
  color: string;
  nmf_known_for: number;
  nmf_interested_in: number;
  watch_threshold_override: number | null;
  surge_threshold_override: number | null;
}

export interface RadarBaselineCurves {
  p25: Record<string, number>;
  p50: Record<string, number>;
  p75: Record<string, number>;
  p90: Record<string, number>;
}

export interface RadarAuthorSummary {
  id: number;
  x_user_id: string;
  handle: string;
  display_name: string;
  avatar_url: string | null;
  followers_count: number;
  is_auto_tracked?: boolean;
  follows_me: boolean;
  followed_by_me: boolean;
  is_mutual: boolean;
  relationship_state: string;
  heavy_hitter_score: number;
  user_cred_v2: number;
  yield_score?: number;
  reply_sample_count?: number;
  avg_reply_views?: number;
  attributed_follows_30d?: number;
  catalysis_lift: number;
  bridge_score: number;
  bridge_role: string;
  is_bridge: boolean;
  communities: RadarCommunityBadge[];
  baseline_curves_json: RadarBaselineCurves;
}

export interface RadarSnapshotPoint {
  id: number;
  age_minutes: number;
  captured_at: string;
  views: number;
  likes: number;
  replies: number;
  quotes: number;
  reposts: number;
  bookmarks: number;
  breakout_score_at_capture: number;
}

export interface RadarCatalystInteraction {
  id: number;
  actor_account_id: number | null;
  actor_x_user_id: string;
  actor_handle: string;
  actor_display_name: string;
  actor_followers: number;
  actor_heavy_hitter_score: number | null;
  interaction_type: string;
  occurred_at: string;
  minutes_after_post: number;
}

export interface RadarXLinks {
  post_url: string;
  reply_intent_url: string;
  quote_intent_url: string;
}

export interface RadarPostCard {
  id: number;
  workspace_id: number;
  x_post_id: string;
  text: string;
  created_at_x: string;
  age_minutes: number;
  queue_status?: QueueStatus;
  acted_at?: string | null;
  acted_action_kind?: ActionKind | null;
  is_reply: boolean;
  is_retweet: boolean;
  is_quote: boolean;
  quoted_post_id: string | null;
  has_url: boolean;
  has_media: boolean;
  media_type: string | null;
  video_duration_ms: number | null;
  matured_48h: boolean;
  latest_views: number;
  latest_likes: number;
  latest_replies: number;
  latest_quotes: number;
  latest_reposts: number;
  latest_bookmarks: number;
  breakout_score: number;
  expected_reply_reach_score?: number;
  breakout_probability: number;
  velocity_z_score: number;
  velocity_multiple: number;
  expected_p50_views: number;
  seismic_infectiousness: number;
  value_model_score: number;
  dpp_novelty_multiplier: number;
  dpp_crowding_discounted: boolean;
  catalysis_boost: number;
  cold_start_eligible: boolean;
  recommended_action: RecommendedAction;
  tactical_rationale: string;
  alert_tier: "surge" | "watch" | "none";
  effective_watch_threshold: number;
  effective_surge_threshold: number;
  hover_summary?: RadarHoverSummary;
  author: RadarAuthorSummary;
  snapshots: RadarSnapshotPoint[];
  interactions: RadarCatalystInteraction[];
  x_links: RadarXLinks;
}

export interface RadarFeedResponse {
  workspace_id: number;
  my_x_handle: string | null;
  watch_threshold: number;
  surge_threshold: number;
  total_matching: number;
  surge_count: number;
  watch_count: number;
  mutual_count?: number;
  query_ms: number;
  generated_at: string;
  items: RadarPostCard[];
}

export interface RadarFeedFilters {
  community_id: number | null;
  bridges_only: boolean;
  mutuals_only: boolean;
  max_age_minutes: number | null;
  tier: "all" | "watch" | "surge";
  min_score?: number | null;
  include_matured?: boolean;
  live_queue?: boolean;
  saved_only?: boolean;
  limit?: number;
}

export interface SavedRadarPreset {
  id: string;
  name: string;
  filters: RadarFeedFilters;
  created_at: string;
}

export interface ValueModelHeadContribution {
  head: string;
  phoenix_score: number;
  base_weight: number;
  effective_weight: number;
  weighted_contribution: number;
  is_boosted: boolean;
  polarity: "positive" | "negative";
}

export interface ValueModelBreakdownOut {
  raw_weighted_sum: number;
  offset_weighted_score: number;
  negative_scores_offset: number;
  oon_multiplier: number;
  author_diversity_multiplier: number;
  combined_post_fusion_multiplier: number;
  final_value_score: number;
  bidirectional_boost_active: boolean;
  in_network: boolean;
  heads: ValueModelHeadContribution[];
  top_positive_heads: ValueModelHeadContribution[];
  negative_heads: ValueModelHeadContribution[];
}

export interface TrajectoryComparisonPoint {
  age_minutes: number;
  p25: number;
  p50: number;
  p75: number;
  p90: number;
  actual_views: number | null;
  breakout_score: number | null;
}

export interface Stage1FilterReportOut {
  passed_oon_retweet_reply: boolean;
  passed_self_reply_chain: boolean;
  passed_age_48h: boolean;
  cold_start_eligible: boolean;
  stage1_visible: boolean;
  drop_reason: string | null;
}

export interface SeismicDetailOut {
  infectiousness: number;
  weighted_cascade_events: number;
  decayed_cascade_events: number;
  decayed_exposure_views: number;
  regularized_rate_per_view: number;
  cascade_velocity_per_min: number;
  acceleration_factor: number;
  signal_breakdown: Record<string, number>;
}

export interface VelocityDetailOut {
  age_minutes: number;
  views: number;
  expected_p25: number;
  expected_p50: number;
  expected_p75: number;
  expected_p90: number;
  velocity_multiple: number;
  iqr_sigma: number;
  velocity_z_score: number;
}

export interface RadarPostDetailResponse {
  post: RadarPostCard;
  algo_version: {
    param_version_id: number | null;
    label: string;
    upstream_commit_sha: string;
    dpp_theta: number;
    oon_weight_factor: number;
  };
  stage1_filters: Stage1FilterReportOut;
  velocity_detail: VelocityDetailOut;
  seismic_detail: SeismicDetailOut;
  value_model_breakdown: ValueModelBreakdownOut;
  trajectory_comparison: TrajectoryComparisonPoint[];
  predictions: Array<{
    id: number;
    param_version_id: number;
    predicted_at: string;
    post_age_minutes: number;
    breakout_probability: number;
    breakout_score: number;
    alert_tier: string;
    outcome_class: string | null;
    brier_error: number | null;
  }>;
  alert_deliveries: AlertDeliveryItem[];
}

export interface RadarPollResponse {
  workspace_id: number;
  provider: string;
  polled_posts_count: number;
  ingested_new_posts_count: number;
  active_posts_count: number;
  matured_now_count: number;
  snapshots_upserted_count: number;
  predictions_recorded_count: number;
  watch_count: number;
  surge_count: number;
  new_alerts_count: number;
  sse_events_emitted: number;
  webhook_deliveries_attempted: number;
  polled_at: string;
  top_items: RadarPostCard[];
}

export interface AlertDeliveryItem {
  id: number;
  workspace_id: number;
  post_id: number | null;
  event_type: string;
  channel: "sse" | "webhook" | string;
  target_url: string | null;
  alert_tier: AlertTier | string;
  payload_json: Record<string, unknown>;
  status:
    | "delivered"
    | "failed"
    | "suppressed_quiet_hours"
    | "suppressed_rate_limit"
    | string;
  http_status: number | null;
  attempts: number;
  error_message: string | null;
  created_at: string;
}

export interface AlertsListResponse {
  workspace_id: number;
  webhook_secret_configured: boolean;
  configured_webhook_urls: string[];
  quiet_hours_start: string | null;
  quiet_hours_end: string | null;
  max_alerts_per_hour: number;
  total_returned: number;
  items: AlertDeliveryItem[];
}

export interface WebhookTestResponse {
  workspace_id: number;
  event_type: string;
  signature_header: string;
  signature_preview: string;
  webhook_secret_configured: boolean;
  signed?: boolean;
  payload: Record<string, unknown>;
  deliveries: AlertDeliveryItem[];
}

export interface RadarSSEEvent {
  id: string;
  event_type:
    | "connected"
    | "heartbeat"
    | "breakout.watch"
    | "breakout.surge"
    | "radar.polled"
    | "webhook.tested"
    | "algo.upstream_drift_detected"
    | string;
  workspace_id: number;
  timestamp: string;
  data: Record<string, unknown>;
}
