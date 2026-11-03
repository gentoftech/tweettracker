import { ADMIN_TOKEN_STORAGE_KEY, ApiError } from "@/lib/api";
import type {
  ActionKind,
  AlertsListResponse,
  QueueStatus,
  RadarFeedFilters,
  RadarFeedResponse,
  RadarPollResponse,
  RadarPostDetailResponse,
  WebhookTestResponse,
} from "@/lib/radar-types";

const API_BASE =
  process.env.NEXT_PUBLIC_API_URL?.replace(/\/$/, "") || "http://localhost:8000";

function getStoredAdminToken(): string {
  if (typeof window === "undefined") return "";
  return window.localStorage.getItem(ADMIN_TOKEN_STORAGE_KEY) || "";
}

async function radarRequest<T>(
  path: string,
  options: RequestInit = {}
): Promise<T> {
  const headers = new Headers(options.headers || {});
  if (!headers.has("Content-Type") && options.body) {
    headers.set("Content-Type", "application/json");
  }
  const token = getStoredAdminToken();
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }

  const response = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers,
    cache: "no-store",
  });

  if (response.status === 204) {
    return undefined as T;
  }

  let payload: unknown = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }

  if (!response.ok) {
    const detail =
      payload && typeof payload === "object" && "detail" in payload
        ? String((payload as { detail: unknown }).detail)
        : `Radar request failed with HTTP ${response.status}`;
    throw new ApiError(response.status, detail);
  }

  return payload as T;
}

export interface SseTicketResponse {
  ticket: string;
  expires_at: number;
  ttl_seconds: number;
  auth_required: boolean;
}

export const radarApi = {
  getRadarFeed: (workspaceId: number, filters: Partial<RadarFeedFilters> = {}) => {
    const params = new URLSearchParams();
    if (filters.community_id !== null && filters.community_id !== undefined) {
      params.set("community_id", String(filters.community_id));
    }
    if (filters.bridges_only) {
      params.set("bridges_only", "true");
    }
    if (filters.mutuals_only) {
      params.set("mutuals_only", "true");
    }
    if (filters.max_age_minutes !== null && filters.max_age_minutes !== undefined) {
      params.set("max_age_minutes", String(filters.max_age_minutes));
    }
    if (filters.tier && filters.tier !== "all") {
      params.set("tier", filters.tier);
    }
    if (filters.min_score !== null && filters.min_score !== undefined) {
      params.set("min_score", String(filters.min_score));
    }
    if (filters.include_matured) {
      params.set("include_matured", "true");
    }
    if (filters.live_queue) {
      params.set("live_queue", "true");
    }
    if (filters.saved_only) {
      params.set("saved_only", "true");
    }
    if (filters.limit !== null && filters.limit !== undefined) {
      params.set("limit", String(filters.limit));
    }
    const qs = params.toString();
    return radarRequest<RadarFeedResponse>(
      `/api/workspaces/${workspaceId}/radar${qs ? `?${qs}` : ""}`
    );
  },

  getPostDetail: (workspaceId: number, postId: number) =>
    radarRequest<RadarPostDetailResponse>(
      `/api/workspaces/${workspaceId}/radar/posts/${postId}`
    ),

  updatePostQueueStatus: (
    workspaceId: number,
    postId: number,
    status: QueueStatus,
    actionKind?: ActionKind
  ) =>
    radarRequest<{
      workspace_id: number;
      post_id: number;
      queue_status: QueueStatus;
      acted_at: string | null;
      acted_action_kind?: ActionKind | null;
    }>(`/api/workspaces/${workspaceId}/radar/posts/${postId}/queue`, {
      method: "POST",
      body: JSON.stringify(
        actionKind ? { status, action_kind: actionKind } : { status }
      ),
    }),

  pollRadarNow: (workspaceId: number) =>
    radarRequest<RadarPollResponse>(
      `/api/workspaces/${workspaceId}/radar/poll`,
      { method: "POST" }
    ),

  listAlerts: (
    workspaceId: number,
    options: { channel?: string; status?: string; limit?: number } = {}
  ) => {
    const params = new URLSearchParams();
    if (options.channel) params.set("channel", options.channel);
    if (options.status) params.set("status", options.status);
    if (options.limit) params.set("limit", String(options.limit));
    const qs = params.toString();
    return radarRequest<AlertsListResponse>(
      `/api/workspaces/${workspaceId}/alerts${qs ? `?${qs}` : ""}`
    );
  },

  sendTestWebhook: (
    workspaceId: number,
    payload: { target_url?: string | null; note?: string | null } = {}
  ) =>
    radarRequest<WebhookTestResponse>(
      `/api/workspaces/${workspaceId}/webhooks/test`,
      {
        method: "POST",
        body: JSON.stringify(payload),
      }
    ),

  issueSseTicket: () =>
    radarRequest<SseTicketResponse>("/api/auth/sse-ticket", {
      method: "POST",
    }),

  resolveEventsSseUrl: async (workspaceId: number): Promise<string> => {
    const token = getStoredAdminToken();
    if (!token) {
      return `${API_BASE}/api/workspaces/${workspaceId}/events`;
    }
    try {
      const res = await radarRequest<SseTicketResponse>("/api/auth/sse-ticket", {
        method: "POST",
      });
      const qs = res.ticket ? `?ticket=${encodeURIComponent(res.ticket)}` : "";
      return `${API_BASE}/api/workspaces/${workspaceId}/events${qs}`;
    } catch {
      return `${API_BASE}/api/workspaces/${workspaceId}/events`;
    }
  },
};
