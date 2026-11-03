import { ADMIN_TOKEN_STORAGE_KEY, ApiError } from "@/lib/api";
import type {
  ActivateStagedResponse,
  AlgoSyncCheckResponse,
  AlgoSyncStatusResponse,
  DismissCandidateResponse,
  FollowerBracket,
  PromoteCandidateResponse,
  RisingListResponse,
  RisingRefreshResponse,
  RisingSortBy,
  RisingStatusFilter,
} from "./rising-types";

const API_BASE =
  process.env.NEXT_PUBLIC_API_URL?.replace(/\/$/, "") || "http://localhost:8000";

function getStoredAdminToken(): string {
  if (typeof window === "undefined") return "";
  return window.localStorage.getItem(ADMIN_TOKEN_STORAGE_KEY) || "";
}

async function risingRequest<T>(path: string, options: RequestInit = {}): Promise<T> {
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
        : `Request failed with HTTP ${response.status}`;
    throw new ApiError(response.status, detail);
  }

  return payload as T;
}

export interface ListRisingParams {
  communityId?: number | null;
  bracket?: FollowerBracket;
  minEndorsements?: number | null;
  status?: RisingStatusFilter;
  sortBy?: RisingSortBy;
}

export const risingApi = {
  listCandidates: (workspaceId: number, params: ListRisingParams = {}) => {
    const query = new URLSearchParams();
    if (params.communityId != null) {
      query.set("community_id", String(params.communityId));
    }
    if (params.bracket && params.bracket !== "all") {
      query.set("bracket", params.bracket);
    }
    if (params.minEndorsements != null && params.minEndorsements > 0) {
      query.set("min_endorsements", String(params.minEndorsements));
    }
    if (params.status) {
      query.set("status", params.status);
    }
    if (params.sortBy) {
      query.set("sort_by", params.sortBy);
    }
    const qs = query.toString();
    return risingRequest<RisingListResponse>(
      `/api/workspaces/${workspaceId}/rising${qs ? `?${qs}` : ""}`
    );
  },

  refreshCandidates: (workspaceId: number) =>
    risingRequest<RisingRefreshResponse>(`/api/workspaces/${workspaceId}/rising/refresh`, {
      method: "POST",
    }),

  promoteCandidate: (
    workspaceId: number,
    candidateId: number,
    communityIds: number[],
    isTracked = true
  ) =>
    risingRequest<PromoteCandidateResponse>(
      `/api/workspaces/${workspaceId}/rising/${candidateId}/promote`,
      {
        method: "POST",
        body: JSON.stringify({ community_ids: communityIds, is_tracked: isTracked }),
      }
    ),

  dismissCandidate: (workspaceId: number, candidateId: number, restore = false) =>
    risingRequest<DismissCandidateResponse>(
      `/api/workspaces/${workspaceId}/rising/${candidateId}/dismiss`,
      {
        method: "POST",
        body: JSON.stringify({ restore }),
      }
    ),

  getAlgoSyncStatus: () => risingRequest<AlgoSyncStatusResponse>("/api/algo-sync/status"),

  runAlgoSyncCheck: (options: {
    simulateDrift?: boolean;
    autoCreateIssue?: boolean;
    workspaceId?: number | null;
  } = {}) =>
    risingRequest<AlgoSyncCheckResponse>("/api/algo-sync/check", {
      method: "POST",
      body: JSON.stringify({
        simulate_drift: options.simulateDrift ?? false,
        auto_create_issue: options.autoCreateIssue ?? true,
        workspace_id: options.workspaceId ?? null,
      }),
    }),

  activateStagedWeights: (eventId: number, workspaceId?: number | null) =>
    risingRequest<ActivateStagedResponse>(
      `/api/algo-sync/events/${eventId}/activate-staged`,
      {
        method: "POST",
        body: JSON.stringify({ workspace_id: workspaceId ?? null }),
      }
    ),
};
