"use client";

import React, { useCallback, useEffect, useMemo, useState } from "react";
import dynamic from "next/dynamic";
import {
  ListFilter,
  Loader2,
  Plus,
  RefreshCw,
  Search,
  Sparkles,
  UserPlus,
  Users,
  VolumeX,
} from "lucide-react";
import { api } from "@/lib/api";
import { useWorkspace } from "@/components/workspace-context";
import { HoverTip } from "@/components/ui/hover-tip";
import { AccountInspectorDrawer } from "@/components/network/account-inspector-drawer";
import {
  CurateImportModal,
  type ImportModalTab,
} from "@/components/network/curate-import-modal";
import type { NetworkAccount, NetworkGraphResponse } from "@/lib/types";

const SigmaGraphCanvas = dynamic(
  () => import("@/components/network/sigma-graph-canvas"),
  {
    ssr: false,
    loading: () => (
      <div className="flex h-[580px] w-full items-center justify-center rounded-2xl border border-slate-800 bg-[#070c18] text-xs text-slate-400">
        <Loader2 className="mr-2 h-4 w-4 animate-spin text-sky-400" />
        Loading network map...
      </div>
    ),
  }
);

type PeopleFilter = "all" | "top_movers" | "follow_back" | "rising";

function compactNumber(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return String(n);
}

function renderStatusBadge(acct: NetworkAccount) {
  if (acct.is_mutual_20x) {
    return (
      <span className="px-2 py-0.5 rounded-full text-[10px] font-semibold bg-emerald-500/15 border border-emerald-500/35 text-emerald-300">
        Mutual
      </span>
    );
  }
  if (acct.relationship_state === "inbound_unreciprocated") {
    return (
      <span className="px-2 py-0.5 rounded-full text-[10px] font-semibold bg-sky-500/15 border border-sky-500/35 text-sky-300">
        Follow Back
      </span>
    );
  }
  if (acct.is_auto_tracked && acct.followers_count < 50_000) {
    return (
      <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-semibold bg-cyan-500/15 border border-cyan-500/35 text-cyan-300">
        <Sparkles className="h-2.5 w-2.5" />
        Rising
      </span>
    );
  }
  if (acct.is_auto_tracked) {
    return (
      <span className="px-2 py-0.5 rounded-full text-[10px] font-semibold bg-slate-800 border border-slate-700 text-slate-300">
        Auto
      </span>
    );
  }
  return null;
}

export default function NetworkPage() {
  const { activeWorkspace, refreshWorkspaceDetails } = useWorkspace();

  const [peopleFilter, setPeopleFilter] = useState<PeopleFilter>("all");
  const [searchQuery, setSearchQuery] = useState<string>("");
  const [accounts, setAccounts] = useState<NetworkAccount[]>([]);
  const [graphData, setGraphData] = useState<NetworkGraphResponse | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedAccountId, setSelectedAccountId] = useState<number | null>(null);
  const [importModalState, setImportModalState] = useState<{
    open: boolean;
    tab: ImportModalTab;
  }>({ open: false, tab: "handles" });
  const [syncingNetwork, setSyncingNetwork] = useState<boolean>(false);
  const [syncNote, setSyncNote] = useState<string | null>(null);

  const wsId = activeWorkspace?.id ?? null;
  const hasOwnHandle = Boolean(activeWorkspace?.my_x_handle?.trim());

  useEffect(() => {
    setPeopleFilter("all");
    setSearchQuery("");
    setSelectedAccountId(null);
    setSyncNote(null);
  }, [wsId]);

  const loadNetworkData = useCallback(
    async (silent = false) => {
      if (!wsId) return;
      if (!silent) setLoading(true);
      setError(null);
      try {
        const [acctsRes, graphRes] = await Promise.all([
          api.listNetworkAccounts(wsId, {
            sort_by: "heavy_hitter_score",
            sort_dir: "desc",
          }),
          api.getNetworkGraph(wsId, {
            mode: "community",
          }),
        ]);
        setAccounts(acctsRes);
        setGraphData(graphRes);
      } catch (err) {
        setError(
          err instanceof Error ? err.message : "Failed to load network map."
        );
      } finally {
        if (!silent) setLoading(false);
      }
    },
    [wsId]
  );

  async function handleSyncNetwork() {
    if (!wsId || syncingNetwork) return;
    setSyncingNetwork(true);
    setError(null);
    setSyncNote(null);
    try {
      const syncRes = await api.autoSyncNetwork(wsId);
      await Promise.all([loadNetworkData(true), refreshWorkspaceDetails(wsId)]);
      const parts: string[] = [];
      if (syncRes.auto_added_count > 0) {
        parts.push(`+${syncRes.auto_added_count} core-affiliated added`);
      }
      if (syncRes.auto_pruned_count > 0) {
        parts.push(`-${syncRes.auto_pruned_count} low-affinity/inactive pruned`);
      }
      if (syncRes.budget_throttle_mode && syncRes.budget_throttle_mode !== "normal") {
        parts.push(`unit pacing: ${syncRes.budget_throttle_mode}`);
      }
      setSyncNote(
        parts.length > 0
          ? `Network synced (${parts.join(" · ")})`
          : "Network core converged (0 adds · 0 prunes)"
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Network sync failed.");
    } finally {
      setSyncingNetwork(false);
    }
  }

  useEffect(() => {
    void loadNetworkData(false);
  }, [loadNetworkData]);

  const counts = useMemo(() => {
    return {
      all: accounts.length,
      top_movers: accounts.filter((a) => a.community_heavy_hitter_score >= 60).length,
      follow_back: accounts.filter(
        (a) => a.relationship_state === "inbound_unreciprocated"
      ).length,
      rising: accounts.filter(
        (a) => Boolean(a.is_auto_tracked) && a.followers_count < 50_000
      ).length,
    };
  }, [accounts]);

  const displayedPeople = useMemo(() => {
    const q = searchQuery.trim().toLowerCase();
    return accounts.filter((a) => {
      if (peopleFilter === "top_movers" && a.community_heavy_hitter_score < 60) {
        return false;
      }
      if (
        peopleFilter === "follow_back" &&
        a.relationship_state !== "inbound_unreciprocated"
      ) {
        return false;
      }
      if (peopleFilter === "rising" && (!a.is_auto_tracked || a.followers_count >= 50_000)) {
        return false;
      }
      if (q) {
        const hay = `${a.handle} ${a.display_name} ${a.bio}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });
  }, [accounts, peopleFilter, searchQuery]);

  async function handleQuickMute(e: React.MouseEvent, acct: NetworkAccount) {
    e.stopPropagation();
    if (!wsId) return;
    setAccounts((prev) => prev.filter((item) => item.id !== acct.id));
    try {
      await api.updateNetworkAccount(wsId, acct.id, { is_muted: true });
      await Promise.all([loadNetworkData(true), refreshWorkspaceDetails(wsId)]);
    } catch {
      await loadNetworkData(true);
    }
  }

  async function handleQuickFollowBack(e: React.MouseEvent, acct: NetworkAccount) {
    e.stopPropagation();
    if (!wsId) return;
    if (typeof window !== "undefined") {
      window.open(acct.x_profile_url, "_blank", "noopener,noreferrer");
    }
    try {
      await api.updateNetworkAccount(wsId, acct.id, { followed_by_me: true });
      await loadNetworkData(true);
    } catch {
      // ignore
    }
  }

  if (!activeWorkspace) {
    return (
      <div className="rounded-2xl border border-slate-800 bg-surface-900/70 p-8 text-center text-sm text-slate-400">
        Loading workspace...
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {/* Clean Single Top Bar: Network Summary + Sync Network / Sync X List / Add People */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-2 text-xs text-slate-300">
          <span className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full bg-surface-900 border border-slate-800 font-medium text-slate-200">
            <Users className="h-3.5 w-3.5 text-sky-400" />
            <span>
              Tracked Network ({activeWorkspace.tracked_accounts_count})
            </span>
          </span>
          {syncNote && (
            <span className="inline-flex items-center px-2.5 py-1 rounded-full bg-emerald-500/10 border border-emerald-500/30 text-[11px] text-emerald-300">
              {syncNote}
            </span>
          )}
        </div>

        <div className="flex items-center gap-2">
          <button
            type="button"
            disabled={syncingNetwork}
            onClick={() => void handleSyncNetwork()}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-xl bg-surface-900 hover:bg-surface-800 border border-slate-800 text-xs font-medium text-slate-300 transition-colors disabled:opacity-50"
            title="Learn high-yield core voices from your replies and prune low-yield accounts (28d grace for seeds, 48h for auto-adds)"
          >
            <RefreshCw
              className={`h-3.5 w-3.5 text-sky-400 ${
                syncingNetwork ? "animate-spin" : ""
              }`}
            />
            <span>{syncingNetwork ? "Syncing..." : "Sync Network"}</span>
          </button>
          <button
            type="button"
            onClick={() => setImportModalState({ open: true, tab: "x_list" })}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-xl bg-surface-900 hover:bg-surface-800 border border-slate-800 text-xs font-medium text-slate-300 transition-colors"
          >
            <ListFilter className="h-3.5 w-3.5 text-slate-400" />
            <span>Sync X List</span>
          </button>
          <button
            type="button"
            onClick={() => setImportModalState({ open: true, tab: "handles" })}
            className="inline-flex items-center gap-1.5 px-3.5 py-1.5 rounded-xl bg-sky-500 hover:bg-sky-400 text-slate-950 text-xs font-semibold transition-colors"
          >
            <Plus className="h-3.5 w-3.5" />
            <span>Add People</span>
          </button>
        </div>
      </div>

      {error && (
        <div className="rounded-xl border border-rose-500/40 bg-rose-950/30 px-4 py-3 text-xs text-rose-200">
          {error}
        </div>
      )}

      {/* Unified Side-by-Side Map + People List */}
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-5 items-start">
        {/* Left: Interactive Network Graph */}
        <div className="lg:col-span-7">
          {loading || !graphData ? (
            <div className="flex h-[580px] items-center justify-center rounded-2xl border border-slate-800 bg-[#070c18] text-xs text-slate-400">
              <Loader2 className="mr-2 h-4 w-4 animate-spin text-sky-400" />
              Mapping network connections...
            </div>
          ) : (
            <SigmaGraphCanvas
              graphData={graphData}
              mode="community"
              sizeBy="heavy_hitter"
              highlightFilter={peopleFilter}
              searchQuery={searchQuery}
              selectedAccountId={selectedAccountId}
              onSelectAccount={(aid) => setSelectedAccountId(aid)}
            />
          )}
        </div>

        {/* Right: Unified People Directory */}
        <div className="lg:col-span-5 rounded-2xl border border-slate-800 bg-surface-900/90 flex flex-col h-[580px] overflow-hidden">
          {/* Search + Filter Pills */}
          <div className="p-3.5 border-b border-slate-800 space-y-2.5">
            <div className="relative">
              <Search className="h-3.5 w-3.5 text-slate-500 absolute left-3 top-1/2 -translate-y-1/2" />
              <input
                type="text"
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                placeholder="Search people by name, @handle, or bio..."
                className="w-full rounded-xl bg-surface-950 border border-slate-800 pl-8 pr-3 py-1.5 text-xs text-white placeholder:text-slate-500 focus:outline-none focus:border-sky-500"
              />
            </div>

            <div className="flex items-center gap-1.5 flex-wrap">
              <button
                type="button"
                onClick={() => setPeopleFilter("all")}
                className={`px-2.5 py-1 rounded-lg text-[11px] font-medium transition-colors ${
                  peopleFilter === "all"
                    ? "bg-slate-100 text-slate-950"
                    : "bg-surface-850 text-slate-300 hover:bg-surface-800"
                }`}
              >
                All ({counts.all})
              </button>
              <button
                type="button"
                onClick={() => setPeopleFilter("top_movers")}
                className={`px-2.5 py-1 rounded-lg text-[11px] font-medium transition-colors ${
                  peopleFilter === "top_movers"
                    ? "bg-amber-400/20 text-amber-200 border border-amber-400/40"
                    : "bg-surface-850 text-slate-300 hover:bg-surface-800"
                }`}
              >
                Top Movers ({counts.top_movers})
              </button>
              {hasOwnHandle && counts.follow_back > 0 && (
                <button
                  type="button"
                  onClick={() => setPeopleFilter("follow_back")}
                  className={`px-2.5 py-1 rounded-lg text-[11px] font-medium transition-colors ${
                    peopleFilter === "follow_back"
                      ? "bg-sky-400/20 text-sky-200 border border-sky-400/40"
                      : "bg-surface-850 text-slate-300 hover:bg-surface-800"
                  }`}
                >
                  Follow Back ({counts.follow_back})
                </button>
              )}
              <button
                type="button"
                onClick={() => setPeopleFilter("rising")}
                className={`inline-flex items-center gap-1 px-2.5 py-1 rounded-lg text-[11px] font-medium transition-colors ${
                  peopleFilter === "rising"
                    ? "bg-cyan-400/20 text-cyan-200 border border-cyan-400/40"
                    : "bg-surface-850 text-slate-300 hover:bg-surface-800"
                }`}
                title="Auto-tracked <50k voices co-signed by your network (auto-cleaned if inactive for 30 days)"
              >
                <Sparkles className="h-3 w-3 text-cyan-400" />
                <span>Rising ({counts.rising})</span>
              </button>
            </div>
          </div>

          {/* People Rows */}
          <div className="flex-1 overflow-y-auto divide-y divide-slate-800/70">
            {displayedPeople.length === 0 ? (
              <div className="p-8 text-center text-xs text-slate-400">
                No people match this filter.
              </div>
            ) : (
              displayedPeople.map((acct) => {
                const influence = Math.round(acct.community_heavy_hitter_score);
                const isSelected = selectedAccountId === acct.id;

                const hoverContent = (
                  <div className="space-y-1.5">
                    <div className="flex items-center justify-between gap-4 border-b border-slate-800 pb-1">
                      <span className="font-semibold text-white">
                        Influence {influence}/100
                      </span>
                      <span className="text-[11px] text-slate-400">
                        {compactNumber(acct.followers_count)} followers
                      </span>
                    </div>
                    <div className="text-[11px] text-amber-300">
                      Spark Lift: {acct.catalysis_lift.toFixed(1)}x reach boost when they reply or quote
                    </div>
                    {typeof acct.yield_score === "number" && (
                      <div className="text-[11px] text-violet-300">
                        Reply Yield: {Math.round(acct.yield_score * 100)}%
                        {(acct.reply_sample_count ?? 0) > 0
                          ? ` · ~${compactNumber(Math.round(acct.avg_reply_views ?? 0))} avg views (${acct.reply_sample_count} ${
                              acct.reply_sample_count === 1 ? "reply" : "replies"
                            })`
                          : " (reach prior)"}
                      </div>
                    )}
                    {typeof acct.core_affinity_score === "number" && (
                      <div className="text-[11px] text-sky-300">
                        Core Affinity: {Math.round(acct.core_affinity_score * 100)}% · UserCredV2{" "}
                        {Math.round(acct.user_cred_v2)}
                      </div>
                    )}
                    <div className="text-[10px] text-cyan-300/90 pt-0.5">
                      {acct.is_auto_tracked
                        ? "Auto-tracked · rises into core with reply reach or prunes after 48h if low-yield"
                        : "Bootstrap seed (28d grace) · stays in core via reply reach & climbing posts"}
                    </div>
                  </div>
                );

                return (
                  <div
                    key={acct.id}
                    onClick={() => setSelectedAccountId(acct.id)}
                    className={`px-3.5 py-2.5 flex items-center justify-between gap-3 cursor-pointer transition-colors ${
                      isSelected ? "bg-sky-500/10" : "hover:bg-surface-850/90"
                    }`}
                  >
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-1.5 flex-wrap">
                        <span
                          className="h-2 w-2 rounded-full shrink-0"
                          style={{
                            backgroundColor: acct.primary_community_color || "#38bdf8",
                          }}
                          title="Auto-detected interaction cluster"
                        />
                        <span className="text-xs font-semibold text-white truncate max-w-[150px]">
                          {acct.display_name}
                        </span>
                        <a
                          href={`https://x.com/${acct.handle}`}
                          target="_blank"
                          rel="noopener noreferrer"
                          onClick={(e) => e.stopPropagation()}
                          className="text-[11px] text-slate-400 truncate hover:text-sky-300 hover:underline"
                          title={`Open @${acct.handle} on X`}
                        >
                          @{acct.handle}
                        </a>
                      </div>

                      <div className="mt-1 flex items-center gap-2">
                        {renderStatusBadge(acct)}
                        <span className="text-[11px] text-slate-500 truncate">
                          {compactNumber(acct.followers_count)} followers
                        </span>
                      </div>
                    </div>

                    <div className="flex items-center gap-1.5 shrink-0">
                      {acct.relationship_state === "inbound_unreciprocated" && (
                        <button
                          type="button"
                          onClick={(e) => void handleQuickFollowBack(e, acct)}
                          className="p-1.5 rounded-lg bg-sky-500/15 hover:bg-sky-500/25 border border-sky-500/30 text-sky-300"
                          title="Follow back on X"
                        >
                          <UserPlus className="h-3.5 w-3.5" />
                        </button>
                      )}

                      {acct.is_auto_tracked && (
                        <button
                          type="button"
                          onClick={(e) => void handleQuickMute(e, acct)}
                          className="p-1.5 rounded-lg hover:bg-rose-500/15 text-slate-500 hover:text-rose-300 transition-colors"
                          title="Mute forever"
                        >
                          <VolumeX className="h-3.5 w-3.5" />
                        </button>
                      )}

                      <HoverTip content={hoverContent} side="left" align="center">
                        <div className="min-w-[44px] text-right rounded-lg bg-surface-950 border border-slate-800 px-2 py-1">
                          <div className="text-sm font-bold tabular-nums text-slate-100 leading-none">
                            {influence}
                          </div>
                        </div>
                      </HoverTip>
                    </div>
                  </div>
                );
              })
            )}
          </div>
        </div>
      </div>

      {/* Person Drawer */}
      <AccountInspectorDrawer
        workspaceId={activeWorkspace.id}
        accountId={selectedAccountId}
        onClose={() => setSelectedAccountId(null)}
        onAccountMutated={async () => {
          await Promise.all([
            loadNetworkData(true),
            refreshWorkspaceDetails(activeWorkspace.id),
          ]);
        }}
      />

      {/* Add People Modal */}
      <CurateImportModal
        open={importModalState.open}
        initialTab={importModalState.tab}
        onClose={() => setImportModalState((s) => ({ ...s, open: false }))}
        onCompleted={async () => {
          await Promise.all([
            loadNetworkData(true),
            refreshWorkspaceDetails(activeWorkspace.id),
          ]);
        }}
      />
    </div>
  );
}
