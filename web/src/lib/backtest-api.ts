import { ADMIN_TOKEN_STORAGE_KEY, ApiError } from "@/lib/api";
import type {
  ActivateVersionResponse,
  BacktestPromoteResponse,
  BacktestReplayResponse,
  CalibrateResult,
  ChangelogResponse,
  SandboxCandidateParams,
  PerformanceResponse,
  ScorecardResponse,
} from "./backtest-types";

const API_BASE =
  process.env.NEXT_PUBLIC_API_URL?.replace(/\/$/, "") || "http://localhost:8000";

function getStoredAdminToken(): string {
  if (typeof window === "undefined") return "";
  return window.localStorage.getItem(ADMIN_TOKEN_STORAGE_KEY) || "";
}

async function backtestRequest<T>(
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
        : `Request failed with HTTP ${response.status}`;
    throw new ApiError(response.status, detail);
  }

  return payload as T;
}

export const backtestApi = {
  getScorecard: (workspaceId: number) =>
    backtestRequest<ScorecardResponse>(`/api/workspaces/${workspaceId}/scorecard`),

  getPerformance: (workspaceId: number) =>
    backtestRequest<PerformanceResponse>(`/api/workspaces/${workspaceId}/performance`),

  gradeScorecard: (
    workspaceId: number,
    options?: { breakout_multiple?: number; community_floor_views?: number }
  ) =>
    backtestRequest<ScorecardResponse>(
      `/api/workspaces/${workspaceId}/scorecard/grade`,
      {
        method: "POST",
        body: JSON.stringify(options ?? {}),
      }
    ),

  getChangelog: (workspaceId: number) =>
    backtestRequest<ChangelogResponse>(`/api/workspaces/${workspaceId}/changelog`),

  calibrateWorkspace: (
    workspaceId: number,
    options?: {
      min_matured_posts?: number;
      l2_reg?: number;
      max_drift_pct?: number;
      force_stage_only?: boolean;
    }
  ) =>
    backtestRequest<CalibrateResult>(`/api/workspaces/${workspaceId}/calibrate`, {
      method: "POST",
      body: JSON.stringify(options ?? {}),
    }),

  activateVersion: (workspaceId: number, versionId: number, reason?: string) =>
    backtestRequest<ActivateVersionResponse>(
      `/api/workspaces/${workspaceId}/changelog/${versionId}/activate`,
      {
        method: "POST",
        body: JSON.stringify(reason ? { reason } : {}),
      }
    ),

  runReplay: (workspaceId: number, params: SandboxCandidateParams) =>
    backtestRequest<BacktestReplayResponse>(
      `/api/workspaces/${workspaceId}/backtest/replay`,
      {
        method: "POST",
        body: JSON.stringify(params),
      }
    ),

  promoteCandidate: (workspaceId: number, params: SandboxCandidateParams) =>
    backtestRequest<BacktestPromoteResponse>(
      `/api/workspaces/${workspaceId}/backtest/promote`,
      {
        method: "POST",
        body: JSON.stringify(params),
      }
    ),
};
