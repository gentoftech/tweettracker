"use client";

import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import { ADMIN_TOKEN_STORAGE_KEY, ApiError, api } from "@/lib/api";
import type {
  AlgoParamsResponse,
  HealthResponse,
  SpendSummary,
  Workspace,
  WorkspaceCreatePayload,
  WorkspaceOverview,
  WorkspaceUpdatePayload,
} from "@/lib/types";

const ACTIVE_WS_STORAGE_KEY = "tweettracker_active_workspace_id";

interface WorkspaceContextValue {
  workspaces: Workspace[];
  activeWorkspace: Workspace | null;
  spendSummary: SpendSummary | null;
  overview: WorkspaceOverview | null;
  algoParams: AlgoParamsResponse | null;
  health: HealthResponse | null;
  loading: boolean;
  error: string | null;
  authRequired: boolean;
  isAuthenticated: boolean;
  showAuthDialog: boolean;
  setShowAuthDialog: (open: boolean) => void;
  showNewWorkspaceModal: boolean;
  setShowNewWorkspaceModal: (open: boolean) => void;
  selectWorkspace: (id: number) => void;
  refreshAll: () => Promise<void>;
  refreshWorkspaceDetails: (workspaceId: number) => Promise<void>;
  createWorkspace: (payload: WorkspaceCreatePayload) => Promise<Workspace>;
  updateWorkspace: (payload: WorkspaceUpdatePayload) => Promise<Workspace>;
  deleteWorkspace: (workspaceId: number) => Promise<void>;
  reconcileSpend: () => Promise<void>;
  loginWithPassword: (password: string) => Promise<void>;
  clearAdminSession: () => void;
}

const WorkspaceContext = createContext<WorkspaceContextValue | undefined>(undefined);

export function WorkspaceProvider({ children }: { children: React.ReactNode }) {
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [activeWorkspaceId, setActiveWorkspaceId] = useState<number | null>(null);
  const [spendSummary, setSpendSummary] = useState<SpendSummary | null>(null);
  const [overview, setOverview] = useState<WorkspaceOverview | null>(null);
  const [algoParams, setAlgoParams] = useState<AlgoParamsResponse | null>(null);
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  const [authRequired, setAuthRequired] = useState<boolean>(false);
  const [isAuthenticated, setIsAuthenticated] = useState<boolean>(true);
  const [showAuthDialog, setShowAuthDialog] = useState<boolean>(false);
  const [showNewWorkspaceModal, setShowNewWorkspaceModal] = useState<boolean>(false);

  const activeWorkspace = useMemo(() => {
    if (!workspaces.length) return null;
    return workspaces.find((w) => w.id === activeWorkspaceId) ?? workspaces[0];
  }, [workspaces, activeWorkspaceId]);

  const refreshWorkspaceDetails = useCallback(async (workspaceId: number) => {
    try {
      const [spend, ov, algo] = await Promise.all([
        api.getWorkspaceSpend(workspaceId),
        api.getWorkspaceOverview(workspaceId),
        api.getAlgoParams(workspaceId),
      ]);
      setSpendSummary(spend);
      setOverview(ov);
      setAlgoParams(algo);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setIsAuthenticated(false);
        setShowAuthDialog(true);
      }
    }
  }, []);

  const refreshAll = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [healthRes, authRes] = await Promise.all([
        api.getHealth(),
        api.getAuthStatus(),
      ]);
      setHealth(healthRes);
      setAuthRequired(authRes.auth_required);
      setIsAuthenticated(authRes.authenticated);

      if (authRes.auth_required && !authRes.authenticated) {
        setShowAuthDialog(true);
        setLoading(false);
        return;
      }

      const wsList = await api.listWorkspaces();
      setWorkspaces(wsList);

      const storedRaw =
        typeof window !== "undefined"
          ? window.localStorage.getItem(ACTIVE_WS_STORAGE_KEY)
          : null;
      const storedId = storedRaw ? Number(storedRaw) : null;
      const chosen =
        wsList.find((w) => w.id === storedId) ??
        wsList.find((w) => w.id === activeWorkspaceId) ??
        wsList[0] ??
        null;

      if (chosen) {
        setActiveWorkspaceId(chosen.id);
        if (typeof window !== "undefined") {
          window.localStorage.setItem(ACTIVE_WS_STORAGE_KEY, String(chosen.id));
        }
        await refreshWorkspaceDetails(chosen.id);
      }
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setAuthRequired(true);
        setIsAuthenticated(false);
        setShowAuthDialog(true);
      } else {
        setError(
          err instanceof Error
            ? err.message
            : "Unable to reach TweetTracker API on port 8000."
        );
      }
    } finally {
      setLoading(false);
    }
  }, [activeWorkspaceId, refreshWorkspaceDetails]);

  useEffect(() => {
    void refreshAll();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const selectWorkspace = useCallback(
    (id: number) => {
      setActiveWorkspaceId(id);
      if (typeof window !== "undefined") {
        window.localStorage.setItem(ACTIVE_WS_STORAGE_KEY, String(id));
      }
      void refreshWorkspaceDetails(id);
    },
    [refreshWorkspaceDetails]
  );

  const createWorkspace = useCallback(
    async (payload: WorkspaceCreatePayload) => {
      const created = await api.createWorkspace(payload);
      const wsList = await api.listWorkspaces();
      setWorkspaces(wsList);
      setActiveWorkspaceId(created.id);
      if (typeof window !== "undefined") {
        window.localStorage.setItem(ACTIVE_WS_STORAGE_KEY, String(created.id));
      }
      await refreshWorkspaceDetails(created.id);
      return created;
    },
    [refreshWorkspaceDetails]
  );

  const updateWorkspace = useCallback(
    async (payload: WorkspaceUpdatePayload) => {
      if (!activeWorkspace) {
        throw new Error("No active workspace selected.");
      }
      const updated = await api.updateWorkspace(activeWorkspace.id, payload);
      setWorkspaces((prev) => prev.map((w) => (w.id === updated.id ? updated : w)));
      await refreshWorkspaceDetails(updated.id);
      return updated;
    },
    [activeWorkspace, refreshWorkspaceDetails]
  );

  const deleteWorkspace = useCallback(
    async (workspaceId: number) => {
      await api.deleteWorkspace(workspaceId);
      const remaining = await api.listWorkspaces();
      setWorkspaces(remaining);
      const next = remaining[0] ?? null;
      setActiveWorkspaceId(next ? next.id : null);
      if (next) {
        await refreshWorkspaceDetails(next.id);
      } else {
        setSpendSummary(null);
        setOverview(null);
      }
    },
    [refreshWorkspaceDetails]
  );

  const reconcileSpend = useCallback(async () => {
    if (!activeWorkspace) return;
    const summary = await api.reconcileWorkspaceSpend(activeWorkspace.id);
    setSpendSummary(summary);
  }, [activeWorkspace]);

  const loginWithPassword = useCallback(
    async (password: string) => {
      await api.verifyPassword(password);
      if (typeof window !== "undefined") {
        window.localStorage.setItem(ADMIN_TOKEN_STORAGE_KEY, password);
      }
      setIsAuthenticated(true);
      setShowAuthDialog(false);
      await refreshAll();
    },
    [refreshAll]
  );

  const clearAdminSession = useCallback(() => {
    if (typeof window !== "undefined") {
      window.localStorage.removeItem(ADMIN_TOKEN_STORAGE_KEY);
    }
    if (authRequired) {
      setIsAuthenticated(false);
      setShowAuthDialog(true);
    }
  }, [authRequired]);

  const value = useMemo<WorkspaceContextValue>(
    () => ({
      workspaces,
      activeWorkspace,
      spendSummary,
      overview,
      algoParams,
      health,
      loading,
      error,
      authRequired,
      isAuthenticated,
      showAuthDialog,
      setShowAuthDialog,
      showNewWorkspaceModal,
      setShowNewWorkspaceModal,
      selectWorkspace,
      refreshAll,
      refreshWorkspaceDetails,
      createWorkspace,
      updateWorkspace,
      deleteWorkspace,
      reconcileSpend,
      loginWithPassword,
      clearAdminSession,
    }),
    [
      workspaces,
      activeWorkspace,
      spendSummary,
      overview,
      algoParams,
      health,
      loading,
      error,
      authRequired,
      isAuthenticated,
      showAuthDialog,
      showNewWorkspaceModal,
      selectWorkspace,
      refreshAll,
      refreshWorkspaceDetails,
      createWorkspace,
      updateWorkspace,
      deleteWorkspace,
      reconcileSpend,
      loginWithPassword,
      clearAdminSession,
    ]
  );

  return (
    <WorkspaceContext.Provider value={value}>{children}</WorkspaceContext.Provider>
  );
}

export function useWorkspace(): WorkspaceContextValue {
  const ctx = useContext(WorkspaceContext);
  if (!ctx) {
    throw new Error("useWorkspace must be used inside WorkspaceProvider");
  }
  return ctx;
}
