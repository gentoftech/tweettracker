export type XDataProviderName =
  | "fixture"
  | "tweetapi_com"
  | "socialdata"
  | "twitterapi_io"
  | "official_x";

export type LLMProviderName =
  | "google_gemini"
  | "xai_grok"
  | "openai"
  | "anthropic"
  | "deepinfra";

export interface Community {
  id: number;
  workspace_id: number;
  name: string;
  slug: string;
  color: string;
  description: string;
  x_list_id: string | null;
  watch_threshold_override: number | null;
  surge_threshold_override: number | null;
  sort_order: number;
  accounts_count: number;
}

export interface CommunityCreatePayload {
  name: string;
  color: string;
  description?: string;
  x_list_id?: string | null;
  watch_threshold_override?: number | null;
  surge_threshold_override?: number | null;
}

export interface CommunityUpdatePayload {
  name?: string;
  color?: string;
  description?: string;
  x_list_id?: string | null;
  watch_threshold_override?: number | null;
  surge_threshold_override?: number | null;
  sort_order?: number;
}

export interface Workspace {
  id: number;
  name: string;
  slug: string;
  my_x_handle: string | null;
  x_data_provider: XDataProviderName;
  llm_provider: LLMProviderName;
  radar_poll_minutes: number;
  use_x_lists: boolean;
  x_list_ids_json?: string[];
  monthly_unit_budget?: number;
  hot_posts_min_projected_views?: number;
  hot_posts_min_momentum_score?: number;
  daily_spend_cap_usd: number;
  monthly_spend_cap_usd: number;
  watch_threshold: number;
  surge_threshold: number;
  quiet_hours_start: string | null;
  quiet_hours_end: string | null;
  max_alerts_per_hour: number;
  webhook_urls_json: string[];
  created_at: string;
  updated_at: string;
  communities: Community[];
  tracked_accounts_count: number;
  posts_count: number;
}

export interface WorkspaceCreatePayload {
  name: string;
  my_x_handle?: string | null;
  x_data_provider?: XDataProviderName;
  llm_provider?: LLMProviderName;
  radar_poll_minutes?: number;
  use_x_lists?: boolean;
  x_list_ids_json?: string[];
  monthly_unit_budget?: number;
  hot_posts_min_projected_views?: number;
  hot_posts_min_momentum_score?: number;
  daily_spend_cap_usd?: number;
  monthly_spend_cap_usd?: number;
  initial_communities?: CommunityCreatePayload[];
}

export interface WorkspaceUpdatePayload {
  name?: string;
  my_x_handle?: string | null;
  x_data_provider?: XDataProviderName;
  llm_provider?: LLMProviderName;
  radar_poll_minutes?: number;
  use_x_lists?: boolean;
  x_list_ids_json?: string[];
  monthly_unit_budget?: number;
  hot_posts_min_projected_views?: number;
  hot_posts_min_momentum_score?: number;
  daily_spend_cap_usd?: number;
  monthly_spend_cap_usd?: number;
  watch_threshold?: number;
  surge_threshold?: number;
  quiet_hours_start?: string | null;
  quiet_hours_end?: string | null;
  max_alerts_per_hour?: number;
  webhook_urls_json?: string[];
}

export interface SpendLedgerEntry {
  id: number;
  workspace_id: number | null;
  provider: string;
  endpoint: string;
  units_consumed: number;
  estimated_usd: number;
  reconciled_balance_units: number | null;
  created_at: string | null;
}

export interface SpendSummary {
  workspace_id: number;
  x_data_provider: XDataProviderName;
  llm_provider: LLMProviderName;
  daily_spent_usd: number;
  daily_spend_cap_usd: number;
  daily_pct: number;
  monthly_spent_usd: number;
  monthly_spend_cap_usd: number;
  monthly_pct: number;
  units_today: number;
  units_month: number;
  monthly_unit_budget?: number;
  projected_monthly_units?: number;
  projected_monthly_pct?: number;
  discovery_throttle_mode?: "normal" | "throttled" | "paused" | string;
  reconciled_balance_units: number | null;
  x_provider_configured: boolean;
  llm_provider_configured: boolean;
  provider_credentials_status: Record<string, boolean>;
  recent_ledger: SpendLedgerEntry[];
}

export interface LivePostPreview {
  id: number;
  x_post_id: string;
  author_handle: string;
  author_display_name: string;
  text: string;
  breakout_score: number;
  recommended_action: "reply_mutual_20x" | "quote_oon_safe" | "watch";
  latest_views: number;
  dpp_novelty_multiplier: number;
}

export interface WorkspaceOverview {
  workspace_id: number;
  communities_count: number;
  tracked_accounts_count: number;
  mutual_accounts_count: number;
  bridge_accounts_count: number;
  active_posts_count: number;
  matured_posts_count: number;
  rising_candidates_count: number;
  top_live_posts: LivePostPreview[];
}

export interface AlgoParamsResponse {
  upstream_commit_sha: string;
  negative_scores_offset: number;
  active_version: {
    id: number;
    version_number: number;
    label: string;
    source: string;
    upstream_commit_sha: string;
    holdout_metrics: Record<string, number>;
    changelog_note: string;
  } | null;
  vm_ranker_weights: Record<string, number | boolean>;
  home_mixer_weights: Record<string, number | boolean>;
  pipeline_params: Record<string, number | boolean>;
  user_cred_v2: {
    intercept: number;
    slope: number;
    formula: string;
  };
  latest_upstream_sync: {
    base_sha: string;
    head_sha: string;
    status: string;
    detected_at: string;
    structural_summary: string;
  } | null;
}

export interface HealthResponse {
  status: string;
  version: string;
  upstream_commit_sha: string;
  auth_required: boolean;
  providers_configured?: Record<string, boolean>;
}

export type BridgeRole =
  | "provincial"
  | "two_way_bridge"
  | "multi_circle_connector";

export type RelationshipState =
  | "mutual_active"
  | "mutual_dormant"
  | "outbound_warming"
  | "outbound_cold"
  | "inbound_unreciprocated"
  | "none";

export interface CommunitySplit {
  community_id: number;
  community_name: string;
  community_slug: string;
  color: string;
  is_manual_tag: boolean;
  nmf_known_for: number;
  nmf_interested_in: number;
  pct: number;
}

export interface NetworkAccount {
  id: number;
  workspace_id: number;
  x_user_id: string;
  handle: string;
  display_name: string;
  bio: string;
  avatar_url: string | null;
  followers_count: number;
  following_count: number;
  followers_7d_ago: number;
  follows_me: boolean;
  followed_by_me: boolean;
  relationship_state: RelationshipState;
  is_mutual_20x: boolean;
  mutual_interaction_count_30d: number;
  last_interaction_at: string | null;
  heavy_hitter_score: number;
  community_heavy_hitter_score: number;
  catalysis_lift: number;
  catalyzed_posts_count: number;
  s_core_depth: number;
  salsa_authority: number;
  salsa_scout: number;
  user_cred_v2: number;
  follower_efficiency: number;
  bridge_score: number;
  participation_coeff: number;
  burt_brokerage: number;
  bridge_role: BridgeRole;
  sub_pocket_label: string | null;
  primary_community_id: number | null;
  primary_community_name: string | null;
  primary_community_color: string;
  is_auto_tracked?: boolean;
  is_muted?: boolean;
  core_affinity_score?: number;
  yield_score?: number;
  reply_sample_count?: number;
  avg_reply_views?: number;
  attributed_follows_30d?: number;
  influence_score?: number;
  status_badge?: "mutual" | "follow_back" | "rising" | "auto" | string | null;
  x_profile_url: string;
  community_splits: CommunitySplit[];
}

export interface AccountImportPayload {
  handles_text?: string | null;
  csv_text?: string | null;
  handles?: string[];
  community_ids?: number[];
  x_list_id?: string | null;
  recompute?: boolean;
}

export interface AccountImportResult {
  workspace_id: number;
  imported_count: number;
  updated_existing_count: number;
  deduplicated_count: number;
  total_tracked_accounts: number;
  compute_duration_ms: number;
  accounts: NetworkAccount[];
}

export interface AccountUpdatePayload {
  community_ids?: number[];
  followed_by_me?: boolean;
  follows_me?: boolean;
  is_muted?: boolean;
  is_tracked?: boolean;
  display_name?: string;
  bio?: string;
}

export interface AutoSyncNetworkResult {
  workspace_id: number;
  auto_added_count: number;
  auto_pruned_count: number;
  active_tracked_count: number;
  profiles_refreshed?: number;
  own_replies_synced?: number;
  projected_monthly_units?: number;
  monthly_unit_budget?: number;
  budget_throttle_mode?: string;
  add_threshold_used?: number;
  prune_threshold_used?: number;
  auto_added_handles: string[];
  auto_pruned_handles: string[];
}

export interface RecomputeResult {
  workspace_id: number;
  communities_count: number;
  accounts_count: number;
  edges_count: number;
  bridges_count: number;
  mutual_active_count: number;
  compute_duration_ms: number;
}

export interface XYCoord {
  x: number;
  y: number;
}

export interface GraphPole {
  community_id: number;
  name: string;
  slug: string;
  color: string;
  x: number;
  y: number;
  accounts_count: number;
}

export interface GraphNode {
  id: number;
  handle: string;
  display_name: string;
  followers_count: number;
  single_community_xy: XYCoord;
  n_pole_xy: XYCoord;
  color: string;
  primary_community_id: number | null;
  primary_community_name: string | null;
  heavy_hitter_score: number;
  community_heavy_hitter_score: number;
  catalysis_lift: number;
  catalyzed_posts_count: number;
  s_core_depth: number;
  salsa_authority: number;
  salsa_scout: number;
  user_cred_v2: number;
  follower_efficiency: number;
  bridge_score: number;
  participation_coeff: number;
  burt_brokerage: number;
  bridge_role: BridgeRole;
  relationship_state: RelationshipState;
  is_mutual_20x: boolean;
  follows_me: boolean;
  followed_by_me: boolean;
  is_auto_tracked?: boolean;
  is_muted?: boolean;
  core_affinity_score?: number;
  yield_score?: number;
  reply_sample_count?: number;
  avg_reply_views?: number;
  attributed_follows_30d?: number;
  influence_score?: number;
  status_badge?: "mutual" | "follow_back" | "rising" | "auto" | string | null;
  sub_pocket_label: string | null;
  community_mix: CommunitySplit[];
}

export interface GraphEdge {
  id: string;
  source: number;
  target: number;
  weight: number;
  edge_type: string;
  is_mutual: boolean;
  cross_community: boolean;
}

export interface NetworkGraphResponse {
  workspace_id: number;
  mode: "community" | "bridges";
  k_communities: number;
  bridge_map_available: boolean;
  has_own_handle: boolean;
  my_x_handle: string | null;
  selected_community_id: number | null;
  poles: GraphPole[];
  nodes: GraphNode[];
  edges: GraphEdge[];
}

export interface CatalyzedConversation {
  post_id: number;
  x_post_id: string;
  author_handle: string;
  author_display_name: string;
  text: string;
  interaction_type: string;
  occurred_at: string | null;
  latest_views: number;
  observed_lift: number;
  breakout_score: number;
  x_post_url: string;
}

export interface AccountPostSummary {
  post_id: number;
  x_post_id: string;
  text: string;
  created_at_x: string;
  latest_views: number;
  latest_likes: number;
  latest_replies: number;
  latest_quotes: number;
  value_model_score: number;
  breakout_score: number;
  is_quote: boolean;
  x_post_url: string;
}

export interface CrossCommunityThread {
  peer_account_id: number;
  peer_handle: string;
  peer_display_name: string;
  peer_community_name: string;
  peer_community_color: string;
  edge_type: string;
  decayed_weight: number;
  direction: "inbound" | "outbound";
}

export interface AccountDetailResponse {
  account: NetworkAccount;
  catalyzed_conversations: CatalyzedConversation[];
  recent_posts: AccountPostSummary[];
  cross_community_threads: CrossCommunityThread[];
  mutual_history: {
    has_own_handle: boolean;
    my_x_handle: string | null;
    relationship_state: RelationshipState;
    is_mutual_20x: boolean;
    follows_me: boolean;
    followed_by_me: boolean;
    mutual_interaction_count_30d: number;
    last_interaction_at: string | null;
    roi_score: number;
    recommended_play: string;
  };
}

export interface RelationshipRoiItem {
  account: NetworkAccount;
  roi_score: number;
  reciprocation_readiness: number;
  recommended_play: string;
  unlocks_20x_on_follow_back: boolean;
  x_profile_url: string;
}

export interface RelationshipsOverviewResponse {
  workspace_id: number;
  has_own_handle: boolean;
  my_x_handle: string | null;
  bidirectional_reply_weight: number;
  counts: Record<string, number>;
  roi_queue: RelationshipRoiItem[];
  accounts: NetworkAccount[];
}

export interface SecretStatus {
  key_name: string;
  label: string;
  description: string;
  configured: boolean;
  source: "app" | "env" | "none" | string;
  masked_hint: string;
  has_env_fallback: boolean;
  updated_at: string | null;
}

export interface SecretsListResponse {
  secrets: SecretStatus[];
}
