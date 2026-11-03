export type FollowerBracket =
  | "all"
  | "under_2500"
  | "2500_10k"
  | "10k_25k"
  | "25k_50k";

export type RisingStatusFilter = "active" | "promoted" | "dismissed" | "all";

export type RisingSortBy = "combined" | "punch" | "peer_respect" | "growth";

export interface CandidateEndorsement {
  id: number;
  endorser_account_id: number;
  handle: string;
  display_name: string;
  heavy_hitter_score: number;
  salsa_scout: number;
  endorsement_type: "follow" | "quote" | "reply" | string;
  signal_multiplier: number;
  weight_contribution: number;
  source_x_post_id: string | null;
  interaction_url: string;
  occurred_at: string | null;
}

export interface CommunityProjectionSlice {
  community_id: number;
  slug: string;
  name: string;
  color: string;
  weight: number;
}

export interface CandidateTopPost {
  x_post_id: string;
  text: string;
  views: number;
  reach_multiple: number;
  post_url: string;
}

export interface RisingCandidate {
  id: number;
  workspace_id: number;
  x_user_id: string;
  handle: string;
  display_name: string;
  bio: string;
  profile_url: string;
  followers_count: number;
  followers_growth_rate: number;
  follower_bracket: FollowerBracket | string;
  sampled_posts_count: number;
  posts_above_3x_count: number;
  median_reach_multiple: number;
  meets_consistency_gate: boolean;
  punch_above_weight_score: number;
  peer_respect_score: number;
  combined_rising_score: number;
  endorsements_count: number;
  distinct_endorsers_count: number;
  endorsements: CandidateEndorsement[];
  community_projection_json: Record<string, number>;
  community_projection: CommunityProjectionSlice[];
  top_posts_json: CandidateTopPost[];
  status: "active" | "promoted" | "dismissed" | string;
  updated_at: string | null;
}

export interface RisingListResponse {
  workspace_id: number;
  cold_start_follower_cap: number;
  counts: {
    active: number;
    promoted: number;
    dismissed: number;
  };
  candidates: RisingCandidate[];
}

export interface RisingRefreshResponse extends RisingListResponse {
  refreshed_count: number;
}

export interface PromoteCandidateResponse {
  status: "promoted";
  candidate_id: number;
  workspace_id: number;
  tracked_account: {
    id: number;
    handle: string;
    display_name: string;
    followers_count: number;
    is_tracked: boolean;
    user_cred_v2: number;
    bridge_role: string;
    community_ids: number[];
  };
}

export interface DismissCandidateResponse {
  candidate_id: number;
  workspace_id: number;
  handle: string;
  status: "active" | "dismissed" | string;
}

export interface AlgoParamDiffEntry {
  rust_ident: string;
  mapped_field: string;
  target: string;
  file: string;
  old_value: number | boolean;
  new_value: number | boolean;
  pct_change: number | null;
}

export interface StagedAlgoParamVersion {
  id: number;
  workspace_id: number | null;
  version_number: number;
  label: string;
  source: string;
  upstream_commit_sha: string;
  is_active: boolean;
  weights_json: Record<string, number | boolean>;
  thresholds_json: Record<string, unknown>;
  changelog_note: string;
  created_at: string | null;
}

export interface UpstreamAlgoEventItem {
  id: number;
  base_sha: string;
  head_sha: string;
  detected_at: string | null;
  watched_files_changed_json: string[];
  param_diffs_json: AlgoParamDiffEntry[];
  structural_summary: string;
  github_issue_number: number | null;
  github_issue_url: string | null;
  staged_param_version_id: number | null;
  staged_param_version: StagedAlgoParamVersion | null;
  status: "in_sync" | "drift_detected" | "ignored_non_ranking" | "activated" | string;
}

export interface AlgoSyncStatusResponse {
  last_synced_sha: string;
  baseline_sha: string;
  upstream_repo: string;
  target_github_repo: string;
  github_token_configured: boolean;
  github_auth_source: string;
  llm_providers_configured: {
    google_gemini: boolean;
    xai_grok: boolean;
  };
  watched_paths: string[];
  ignored_paths: string[];
  events: UpstreamAlgoEventItem[];
}

export interface AlgoSyncCheckResponse {
  last_synced_sha: string;
  deduplicated: boolean;
  event: UpstreamAlgoEventItem;
  staged_param_version: StagedAlgoParamVersion | null;
}

export interface ActivateStagedResponse {
  last_synced_sha: string;
  event: UpstreamAlgoEventItem;
  activated_param_version: StagedAlgoParamVersion | null;
}
