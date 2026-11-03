import type {
  AccountDetailResponse,
  AccountImportPayload,
  AccountImportResult,
  AccountUpdatePayload,
  AlgoParamsResponse,
  AutoSyncNetworkResult,
  Community,
  CommunityCreatePayload,
  CommunityUpdatePayload,
  HealthResponse,
  NetworkAccount,
  NetworkGraphResponse,
  RecomputeResult,
  RelationshipsOverviewResponse,
  SecretsListResponse,
  SpendSummary,
  Workspace,
  WorkspaceCreatePayload,
  WorkspaceOverview,
  WorkspaceUpdatePayload,
} from "./types";

const API_BASE =
  process.env.NEXT_PUBLIC_API_URL?.replace(/\/$/, "") || "http://localhost:8000";

export const ADMIN_TOKEN_STORAGE_KEY = "tweettracker_admin_token";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

function getStoredAdminToken(): string {
  if (typeof window === "undefined") return "";
  return window.localStorage.getItem(ADMIN_TOKEN_STORAGE_KEY) || "";
}

async function apiRequest<T>(
  path: string,
  options: RequestInit = {},
  overridePassword?: string
): Promise<T> {
  const headers = new Headers(options.headers || {});
  if (!headers.has("Content-Type") && options.body) {
    headers.set("Content-Type", "application/json");
  }
  const token = overridePassword !== undefined ? overridePassword : getStoredAdminToken();
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

export const api = {
  getHealth: () => apiRequest<HealthResponse>("/api/health"),

  getAuthStatus: (password?: string) =>
    apiRequest<{ auth_required: boolean; authenticated: boolean }>(
      "/api/auth/status",
      {},
      password
    ),

  verifyPassword: (password: string) =>
    apiRequest<{ auth_required: boolean; authenticated: boolean }>("/api/auth/verify", {
      method: "POST",
      body: JSON.stringify({ password }),
    }),

  listWorkspaces: () => apiRequest<Workspace[]>("/api/workspaces"),

  createWorkspace: (data: WorkspaceCreatePayload) =>
    apiRequest<Workspace>("/api/workspaces", {
      method: "POST",
      body: JSON.stringify(data),
    }),

  updateWorkspace: (workspaceId: number, data: WorkspaceUpdatePayload) =>
    apiRequest<Workspace>(`/api/workspaces/${workspaceId}`, {
      method: "PATCH",
      body: JSON.stringify(data),
    }),

  deleteWorkspace: (workspaceId: number) =>
    apiRequest<void>(`/api/workspaces/${workspaceId}`, {
      method: "DELETE",
    }),

  getWorkspaceOverview: (workspaceId: number) =>
    apiRequest<WorkspaceOverview>(`/api/workspaces/${workspaceId}/overview`),

  getWorkspaceSpend: (workspaceId: number) =>
    apiRequest<SpendSummary>(`/api/workspaces/${workspaceId}/spend`),

  reconcileWorkspaceSpend: (workspaceId: number) =>
    apiRequest<SpendSummary>(`/api/workspaces/${workspaceId}/spend/reconcile`, {
      method: "POST",
    }),

  createCommunity: (workspaceId: number, data: CommunityCreatePayload) =>
    apiRequest<Community>(`/api/workspaces/${workspaceId}/communities`, {
      method: "POST",
      body: JSON.stringify(data),
    }),

  updateCommunity: (
    workspaceId: number,
    communityId: number,
    data: CommunityUpdatePayload
  ) =>
    apiRequest<Community>(
      `/api/workspaces/${workspaceId}/communities/${communityId}`,
      {
        method: "PATCH",
        body: JSON.stringify(data),
      }
    ),

  deleteCommunity: (workspaceId: number, communityId: number) =>
    apiRequest<void>(`/api/workspaces/${workspaceId}/communities/${communityId}`, {
      method: "DELETE",
    }),

  listNetworkAccounts: (
    workspaceId: number,
    params?: {
      community_id?: number | null;
      bridge_role?: string | null;
      relationship_state?: string | null;
      rising_only?: boolean;
      search?: string | null;
      sort_by?: string;
      sort_dir?: "asc" | "desc";
    }
  ) => {
    const qs = new URLSearchParams();
    if (params?.community_id != null) {
      qs.set("community_id", String(params.community_id));
    }
    if (params?.bridge_role) {
      qs.set("bridge_role", params.bridge_role);
    }
    if (params?.relationship_state) {
      qs.set("relationship_state", params.relationship_state);
    }
    if (params?.rising_only) {
      qs.set("rising_only", "true");
    }
    if (params?.search) {
      qs.set("search", params.search);
    }
    if (params?.sort_by) {
      qs.set("sort_by", params.sort_by);
    }
    if (params?.sort_dir) {
      qs.set("sort_dir", params.sort_dir);
    }
    const suffix = qs.toString() ? `?${qs.toString()}` : "";
    return apiRequest<NetworkAccount[]>(
      `/api/workspaces/${workspaceId}/network/accounts${suffix}`
    );
  },

  importNetworkAccounts: (workspaceId: number, data: AccountImportPayload) =>
    apiRequest<AccountImportResult>(
      `/api/workspaces/${workspaceId}/network/accounts/import`,
      {
        method: "POST",
        body: JSON.stringify(data),
      }
    ),

  updateNetworkAccount: (
    workspaceId: number,
    accountId: number,
    data: AccountUpdatePayload
  ) =>
    apiRequest<NetworkAccount>(
      `/api/workspaces/${workspaceId}/network/accounts/${accountId}`,
      {
        method: "PATCH",
        body: JSON.stringify(data),
      }
    ),

  deleteNetworkAccount: (workspaceId: number, accountId: number) =>
    apiRequest<void>(
      `/api/workspaces/${workspaceId}/network/accounts/${accountId}`,
      {
        method: "DELETE",
      }
    ),

  recomputeNetwork: (workspaceId: number) =>
    apiRequest<RecomputeResult>(
      `/api/workspaces/${workspaceId}/network/recompute`,
      {
        method: "POST",
      }
    ),

  autoSyncNetwork: (workspaceId: number) =>
    apiRequest<AutoSyncNetworkResult>(
      `/api/workspaces/${workspaceId}/network/auto-sync`,
      {
        method: "POST",
      }
    ),

  getNetworkGraph: (
    workspaceId: number,
    params: {
      mode: "community" | "bridges";
      community_id?: number | null;
      max_edges?: number;
    }
  ) => {
    const qs = new URLSearchParams();
    qs.set("mode", params.mode);
    if (params.community_id != null) {
      qs.set("community_id", String(params.community_id));
    }
    if (params.max_edges != null) {
      qs.set("max_edges", String(params.max_edges));
    }
    return apiRequest<NetworkGraphResponse>(
      `/api/workspaces/${workspaceId}/network/graph?${qs.toString()}`
    );
  },

  getNetworkAccountDetail: (workspaceId: number, accountId: number) =>
    apiRequest<AccountDetailResponse>(
      `/api/workspaces/${workspaceId}/network/accounts/${accountId}/detail`
    ),

  getNetworkRelationships: (
    workspaceId: number,
    params?: {
      state?: string | null;
      community_id?: number | null;
    }
  ) => {
    const qs = new URLSearchParams();
    if (params?.state) {
      qs.set("state", params.state);
    }
    if (params?.community_id != null) {
      qs.set("community_id", String(params.community_id));
    }
    const suffix = qs.toString() ? `?${qs.toString()}` : "";
    return apiRequest<RelationshipsOverviewResponse>(
      `/api/workspaces/${workspaceId}/network/relationships${suffix}`
    );
  },

  getAlgoParams: (workspaceId?: number) =>
    apiRequest<AlgoParamsResponse>(
      workspaceId ? `/api/algo/params?workspace_id=${workspaceId}` : "/api/algo/params"
    ),

  getSecrets: () => apiRequest<SecretsListResponse>("/api/settings/secrets"),

  updateSecrets: (secrets: Record<string, string | null>) =>
    apiRequest<SecretsListResponse>("/api/settings/secrets", {
      method: "PUT",
      body: JSON.stringify({ secrets }),
    }),
};
