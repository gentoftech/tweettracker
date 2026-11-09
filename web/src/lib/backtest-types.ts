export interface CalibrationBin {
  bin_index: number;
  bin_lower: number;
  bin_upper: number;
  bin_midpoint: number;
  count: number;
  predicted_prob_mean: number | null;
  observed_breakout_rate: number | null;
  calibration_gap: number | null;
}

export interface CommunityBadgeRef {
  id: number;
  name: string;
  slug: string;
  color: string;
}

export interface PostmortemItem {
  post_id: number;
  x_post_id: string;
  x_status_url: string;
  author_handle: string;
  author_display_name: string;
  communities: CommunityBadgeRef[];
  text: string;
  outcome_class: "fp" | "fn";
  breakout_score: number;
  breakout_probability: number;
  alert_age_minutes: number;
  views_at_alert: number;
  actual_48h_views: number;
  author_median_48h_views: number;
  actual_48h_multiple: number;
  lead_time_growth_share: number;
  lead_time_multiple: number;
  bookmark_to_like_ratio: number;
  quote_rate: number;
  has_url: boolean;
  dpp_novelty_multiplier: number;
  diagnostic_tag: string;
  diagnostic_label: string;
  diagnostic_reason: string;
}

export interface ScorecardMetrics {
  matured_posts_count: number;
  true_breakouts_count?: number;
  tp: number;
  fp: number;
  fn: number;
  tn: number;
  alerts_total: number;
  alerts_per_day: number;
  precision: number;
  recall: number;
  f05_score: number;
  brier_score: number;
  ece: number;
  roc_auc: number;
  spearman_rho: number;
  median_lead_time_growth_share: number;
  median_lead_time_multiple: number;
  median_alert_age_minutes?: number;
  calibration_bins: CalibrationBin[];
}

export interface CommunityScorecardRow {
  community_id: number;
  name: string;
  slug: string;
  color: string;
  effective_watch_threshold: number;
  community_floor_views: number;
  matured_posts_count: number;
  true_breakouts_count: number;
  tp: number;
  fp: number;
  fn: number;
  tn: number;
  precision: number;
  recall: number;
  f05_score: number;
  brier_score: number;
  roc_auc: number;
  median_lead_time_growth_share: number;
  median_lead_time_multiple: number;
}

export interface ScorecardResponse {
  workspace_id: number;
  workspace_name: string;
  evaluated_at: string;
  outcome_definition: {
    breakout_multiple: number;
    community_floor_views: number;
    lifecycle_hours: number;
  };
  active_version: {
    id: number | null;
    version_number: number;
    label: string;
    source: string;
    upstream_commit_sha: string;
    watch_threshold: number;
    surge_threshold: number;
  };
  overall: ScorecardMetrics;
  communities: CommunityScorecardRow[];
  calibration_curve: CalibrationBin[];
  false_alarms_fp: PostmortemItem[];
  missed_breakouts_fn: PostmortemItem[];
  grade_summary?: {
    workspace_id: number;
    matured_posts_graded: number;
    predictions_evaluated: number;
    true_breakouts_count: number;
    tp: number;
    fp: number;
    fn: number;
    tn: number;
  };
}

export interface ParamDiffEntry {
  section: string;
  param: string;
  const_name: string;
  before: number | boolean;
  after: number | boolean;
  pct_change: number | null;
  formatted: string;
}

export interface HoldoutSplitMeta {
  train_count: number;
  holdout_count: number;
  train_start_at: string;
  train_end_at: string;
  holdout_start_at: string;
  holdout_end_at: string;
  zero_future_leakage: boolean;
}

export interface ChangelogVersion {
  id: number;
  workspace_id: number | null;
  version_number: number;
  label: string;
  source: string;
  upstream_commit_sha: string;
  is_active: boolean;
  is_effective_active: boolean;
  weights: Record<string, number | boolean>;
  surrogate_params: Record<string, number>;
  thresholds: Record<string, unknown>;
  holdout_metrics: {
    precision?: number;
    recall?: number;
    f05?: number;
    brier?: number;
    live_brier?: number;
    live_f05?: number;
    matured_posts?: number;
    gate_passed?: boolean;
    promoted?: boolean;
    max_observed_drift_pct?: number;
    split?: HoldoutSplitMeta;
    param_diffs?: ParamDiffEntry[];
    [key: string]: unknown;
  };
  param_diffs: ParamDiffEntry[];
  changelog_note: string;
  created_at: string;
}

export interface ChangelogResponse {
  workspace_id: number;
  active_version_id: number | null;
  versions: ChangelogVersion[];
}

export interface CalibrateResult {
  status: "promoted" | "staged" | "insufficient_data" | "insufficient_breakouts";
  promoted: boolean;
  gate_passed: boolean;
  reason: string;
  matured_posts_count: number;
  min_matured_posts: number;
  max_drift_pct: number;
  max_observed_drift_pct?: number;
  split: HoldoutSplitMeta | null;
  live_train_metrics?: ScorecardMetrics;
  candidate_train_metrics?: ScorecardMetrics;
  live_holdout_metrics: ScorecardMetrics | null;
  candidate_holdout_metrics: ScorecardMetrics | null;
  live_overall_metrics?: ScorecardMetrics;
  candidate_overall_metrics?: ScorecardMetrics;
  param_diffs: ParamDiffEntry[];
  changelog_note: string;
  version: ChangelogVersion | null;
}

export interface ActivateVersionResponse {
  workspace_id: number;
  previous_version_id: number | null;
  active_version_id: number;
  activated_version: ChangelogVersion;
}

export interface SandboxCandidateParams {
  watch_threshold?: number;
  surge_threshold?: number;
  breakout_multiple?: number;
  bidirectional_follow_reply_weight_boost?: number;
  share_via_copy_link?: number;
  quote?: number;
  reply?: number;
  not_interested?: number;
  dpp_theta?: number;
  prior_views_alpha0?: number;
  copy_link_from_bookmark_mult?: number;
  url_dwell_discount?: number;
  benchmark_10k?: boolean;
  benchmark_size?: number;
  label?: string;
  changelog_note?: string;
}

export interface FlippedPostItem {
  post_id: number;
  x_post_id: string;
  x_status_url: string;
  author_handle: string;
  author_display_name: string;
  communities: CommunityBadgeRef[];
  text: string;
  transition: "fn_to_tp" | "fp_to_tn" | "tn_to_fp" | "tp_to_fn";
  live_score: number;
  candidate_score: number;
  delta_score: number;
  live_probability: number;
  candidate_probability: number;
  actual_48h_views: number;
  actual_48h_multiple: number;
  is_true_breakout_48h: boolean;
}

export interface BacktestReplayResponse {
  workspace_id: number;
  benchmark_10k: boolean;
  trajectories_evaluated: number;
  stored_trajectories_count: number;
  elapsed_ms: number;
  elapsed_secs: number;
  throughput_posts_per_sec: number;
  active_version: {
    id: number | null;
    version_number: number;
    label: string;
    source: string;
    upstream_commit_sha: string;
  };
  live_metrics: ScorecardMetrics;
  candidate_metrics: ScorecardMetrics;
  deltas: {
    delta_precision: number;
    delta_recall: number;
    delta_f05: number;
    delta_brier: number;
    delta_alerts_total: number;
    delta_alerts_per_day: number;
  };
  flips_summary: {
    fn_to_tp: number;
    fp_to_tn: number;
    tn_to_fp: number;
    tp_to_fn: number;
  };
  param_diffs: ParamDiffEntry[];
  post_diffs: FlippedPostItem[];
}

export interface BacktestPromoteResponse {
  promoted: boolean;
  version: ChangelogVersion;
  param_diffs: ParamDiffEntry[];
}

export interface PerformanceSuccess {
  window_days: number;
  net_new_followers: number | null;
  prev_net_new_followers: number | null;
  replies_sent: number;
  reply_impressions: number;
  median_views_per_reply: number | null;
  prev_replies_sent: number;
  prev_reply_impressions: number;
  prev_median_views_per_reply: number | null;
}

export interface PerformanceAccuracy {
  window_days: number;
  graded_posts: number;
  median_actual_vs_projected: number | null;
  share_within_2x: number | null;
  median_share_views_still_ahead: number | null;
  pick_lift_window_days: number;
  hot_posts_replies: number;
  other_replies: number;
  hot_posts_median_views: number | null;
  other_median_views: number | null;
  pick_lift: number | null;
  pick_lift_min_samples: number;
}

export interface PerformanceResponse {
  workspace_id: number;
  generated_at: string;
  success: PerformanceSuccess;
  accuracy: PerformanceAccuracy;
}
