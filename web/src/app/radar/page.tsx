"use client";

import React, { useCallback, useEffect, useState } from "react";
import {
  Bookmark,
  CheckCircle2,
  Flame,
  RefreshCw,
  RotateCcw,
  Users,
} from "lucide-react";
import { useWorkspace } from "@/components/workspace-context";
import { BreakoutPostCard } from "@/components/radar/breakout-post-card";
import { radarApi } from "@/lib/radar-api";
import type {
  ActionKind,
  QueueStatus,
  RadarFeedResponse,
  RadarPostCard,
} from "@/lib/radar-types";

export default function HotPostsPage() {
  const { activeWorkspace, refreshWorkspaceDetails } = useWorkspace();

  const [mutualsOnly, setMutualsOnly] = useState<boolean>(false);
  const [savedOnly, setSavedOnly] = useState<boolean>(false);
  const [feed, setFeed] = useState<RadarFeedResponse | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [polling, setPolling] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [expandedPostId, setExpandedPostId] = useState<number | null>(null);
  const [sseConnected, setSseConnected] = useState<boolean>(false);
  const [lastCleared, setLastCleared] = useState<{
    post: RadarPostCard;
    status: "acted" | "skipped" | "saved";
    restoreTo: "open" | "saved";
  } | null>(null);

  const wsId = activeWorkspace?.id ?? null;
  const hasMyHandle = Boolean(activeWorkspace?.my_x_handle?.trim());

  useEffect(() => {
    setMutualsOnly(false);
    setSavedOnly(false);
    setExpandedPostId(null);
    setLastCleared(null);
  }, [wsId]);

  const loadFeed = useCallback(
    async (silent = false) => {
      if (!wsId) return;
      if (!silent) setLoading(true);
      setError(null);
      try {
        const res = savedOnly
          ? await radarApi.getRadarFeed(wsId, { saved_only: true, limit: 100 })
          : await radarApi.getRadarFeed(wsId, {
              mutuals_only: hasMyHandle ? mutualsOnly : false,
              live_queue: true,
              limit: 100,
            });
        setFeed(res);
      } catch (err) {
        setError(
          err instanceof Error ? err.message : "Failed to load Hot Posts queue."
        );
      } finally {
        if (!silent) setLoading(false);
      }
    },
    [wsId, mutualsOnly, savedOnly, hasMyHandle]
  );

  useEffect(() => {
    void loadFeed(false);
  }, [loadFeed]);

  // Auto-refresh every 45s so posts that lose momentum drop automatically
  useEffect(() => {
    if (!wsId) return;
    const interval = window.setInterval(() => {
      void loadFeed(true);
    }, 45_000);
    return () => window.clearInterval(interval);
  }, [wsId, loadFeed]);

  // Live SSE auto-update
  useEffect(() => {
    if (!wsId || typeof window === "undefined") return;
    let es: EventSource | null = null;
    let cancelled = false;

    void (async () => {
      const url = await radarApi.resolveEventsSseUrl(wsId);
      if (cancelled) return;
      es = new EventSource(url);
      es.onopen = () => setSseConnected(true);
      es.onerror = () => setSseConnected(false);
      const refreshHandler = () => {
        void loadFeed(true);
      };
      es.addEventListener("breakout.surge", refreshHandler);
      es.addEventListener("breakout.watch", refreshHandler);
      es.addEventListener("radar.polled", refreshHandler);
    })();

    return () => {
      cancelled = true;
      setSseConnected(false);
      if (es) es.close();
    };
  }, [wsId, loadFeed]);

  async function handleRefreshNow() {
    if (!wsId || polling) return;
    setPolling(true);
    setError(null);
    try {
      await radarApi.pollRadarNow(wsId);
      await Promise.all([loadFeed(true), refreshWorkspaceDetails(wsId)]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Refresh failed.");
    } finally {
      setPolling(false);
    }
  }

  async function handleQueueAction(
    post: RadarPostCard,
    status: QueueStatus,
    actionKind?: ActionKind
  ) {
    if (!wsId) return;
    // Unsave in the Saved tab removes the card without an undo toast
    const removesFromView =
      status === "acted" || status === "skipped" || status === "saved" || savedOnly;
    if (status === "acted" || status === "skipped" || status === "saved") {
      setLastCleared({ post, status, restoreTo: savedOnly ? "saved" : "open" });
    }
    if (removesFromView) {
      setFeed((prev) => {
        if (!prev) return prev;
        const remaining = prev.items.filter((it) => it.id !== post.id);
        return {
          ...prev,
          items: remaining,
          total_matching: remaining.length,
          surge_count: remaining.filter((it) => it.alert_tier === "surge").length,
          watch_count: remaining.filter((it) => it.alert_tier === "watch").length,
          mutual_count: remaining.filter((it) => it.author.is_mutual).length,
        };
      });
    }
    try {
      await radarApi.updatePostQueueStatus(wsId, post.id, status, actionKind);
      if (status === "open" && !savedOnly) {
        await loadFeed(true);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not update queue.");
      await loadFeed(true);
    }
  }

  async function handleUndoLastCleared() {
    if (!wsId || !lastCleared) return;
    const item = lastCleared;
    setLastCleared(null);
    try {
      await radarApi.updatePostQueueStatus(wsId, item.post.id, item.restoreTo);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not undo.");
    }
    await loadFeed(true);
  }

  function selectView(view: "all" | "mutuals" | "saved") {
    setMutualsOnly(view === "mutuals");
    setSavedOnly(view === "saved");
    setLastCleared(null);
    setExpandedPostId(null);
  }

  if (!activeWorkspace) {
    return (
      <div className="rounded-2xl border border-slate-800 bg-surface-900/70 p-8 text-center text-sm text-slate-400">
        Loading workspace...
      </div>
    );
  }

  const items = feed?.items ?? [];
  const totalLive = feed?.total_matching ?? 0;
  const surgingCount = feed?.surge_count ?? 0;
  const mutualCount =
    feed?.mutual_count ?? items.filter((p) => p.author.is_mutual).length;

  return (
    <div className="max-w-4xl mx-auto space-y-4">
      {/* Compact Single Filter & Status Bar */}
      <div className="flex flex-wrap items-center justify-between gap-3 pb-1">
        {/* Left: All Posts / Mutuals / Saved Filter */}
        <div className="flex items-center gap-1.5 flex-wrap">
          <button
            type="button"
            onClick={() => selectView("all")}
            className={`px-3 py-1.5 rounded-full text-xs font-medium border transition-colors ${
              !mutualsOnly && !savedOnly
                ? "bg-slate-100 text-slate-950 border-slate-100"
                : "bg-surface-900 border-slate-800 text-slate-300 hover:border-slate-700"
            }`}
          >
            All Posts
          </button>
          {hasMyHandle && (
            <button
              type="button"
              onClick={() => selectView(mutualsOnly ? "all" : "mutuals")}
              className={`inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full text-xs font-medium border transition-colors ${
                mutualsOnly
                  ? "bg-emerald-500/20 border-emerald-400/50 text-emerald-200"
                  : "bg-surface-900 border-slate-800 text-slate-300 hover:border-slate-700"
              }`}
            >
              <Users className="h-3.5 w-3.5" />
              <span>Mutuals</span>
            </button>
          )}
          <button
            type="button"
            onClick={() => selectView(savedOnly ? "all" : "saved")}
            className={`inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full text-xs font-medium border transition-colors ${
              savedOnly
                ? "bg-amber-500/20 border-amber-400/50 text-amber-200"
                : "bg-surface-900 border-slate-800 text-slate-300 hover:border-slate-700"
            }`}
          >
            <Bookmark className="h-3.5 w-3.5" />
            <span>Saved</span>
          </button>
        </div>

        {/* Right: Inline Live Summary + Refresh */}
        <div className="flex items-center gap-3 text-xs text-slate-400">
          <div className="flex items-center gap-2">
            <span
              className={`h-2 w-2 rounded-full ${
                sseConnected ? "bg-emerald-400 animate-pulse" : "bg-slate-600"
              }`}
              title={sseConnected ? "Live stream connected" : "Connecting live stream"}
            />
            <span>
              <strong className="text-slate-200">{totalLive}</strong>{" "}
              {savedOnly ? "Saved" : "Live"}
            </span>
            {!savedOnly && surgingCount > 0 && (
              <>
                <span>·</span>
                <span className="text-amber-300 font-medium">
                  {surgingCount} Surging
                </span>
              </>
            )}
            {!savedOnly && hasMyHandle && mutualCount > 0 && (
              <>
                <span>·</span>
                <span className="text-emerald-300 font-medium">
                  {mutualCount} Mutual{mutualCount === 1 ? "" : "s"}
                </span>
              </>
            )}
          </div>

          <button
            type="button"
            onClick={() => void handleRefreshNow()}
            disabled={polling}
            className="inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg bg-surface-900 hover:bg-surface-800 border border-slate-800 text-xs font-medium text-slate-300 transition-colors disabled:opacity-50"
            title="Check for new hot posts now"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${polling ? "animate-spin text-sky-400" : ""}`} />
            <span>Refresh</span>
          </button>
        </div>
      </div>

      {/* Inline Undo Toast */}
      {lastCleared && (
        <div className="rounded-xl border border-slate-800 bg-surface-900/95 px-4 py-2.5 text-xs text-slate-300 flex items-center justify-between gap-3">
          <span>
            {lastCleared.status === "acted"
              ? `Opened @${lastCleared.post.author.handle}'s post on X and cleared from queue.`
              : lastCleared.status === "saved"
                ? `Saved @${lastCleared.post.author.handle}'s post for later.`
                : `Skipped @${lastCleared.post.author.handle}'s post.`}
          </span>
          <button
            type="button"
            onClick={() => void handleUndoLastCleared()}
            className="inline-flex items-center gap-1 font-semibold text-sky-400 hover:text-sky-300 shrink-0"
          >
            <RotateCcw className="h-3.5 w-3.5" />
            <span>Undo</span>
          </button>
        </div>
      )}

      {error && (
        <div className="rounded-xl border border-rose-500/40 bg-rose-950/30 px-4 py-3 text-xs text-rose-200">
          {error}
        </div>
      )}

      {/* Priority Queue */}
      {loading ? (
        <div className="rounded-2xl border border-slate-800 bg-surface-900/60 p-10 text-center text-sm text-slate-400 flex items-center justify-center gap-2">
          <RefreshCw className="h-4 w-4 animate-spin text-sky-400" />
          <span>Scanning live conversations...</span>
        </div>
      ) : items.length === 0 && savedOnly ? (
        <div className="rounded-2xl border border-slate-800 bg-surface-900/60 p-12 text-center space-y-2">
          <Bookmark className="h-7 w-7 text-amber-300 mx-auto" />
          <div className="text-sm font-semibold text-slate-200">No saved posts yet</div>
          <p className="text-xs text-slate-400 max-w-md mx-auto">
            Tap Save on a Hot Posts card to keep a post for later. Saved posts stay here after
            they drop out of the live queue.
          </p>
        </div>
      ) : items.length === 0 ? (
        <div className="rounded-2xl border border-slate-800 bg-surface-900/60 p-12 text-center space-y-2">
          <CheckCircle2 className="h-7 w-7 text-emerald-400 mx-auto" />
          <div className="text-sm font-semibold text-slate-200">
            You&apos;re all caught up
          </div>
          <p className="text-xs text-slate-400 max-w-md mx-auto">
            No posts in {mutualsOnly ? "your mutual network" : "your tracked network"} are surging right now. New breakouts appear here automatically as they gain traction, and stalled posts clear themselves out.
          </p>
        </div>
      ) : (
        <div className="space-y-3">
          {items.map((post) => (
            <BreakoutPostCard
              key={post.id}
              workspaceId={activeWorkspace.id}
              post={post}
              expanded={expandedPostId === post.id}
              onToggleExpand={() =>
                setExpandedPostId((prev) => (prev === post.id ? null : post.id))
              }
              onQueueAction={handleQueueAction}
              savedView={savedOnly}
            />
          ))}
        </div>
      )}
    </div>
  );
}
