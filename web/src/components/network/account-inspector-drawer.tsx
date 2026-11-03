"use client";

import React, { useCallback, useEffect, useState } from "react";
import {
  ExternalLink,
  Flame,
  Loader2,
  Sparkles,
  Trash2,
  UserCheck,
  UserPlus,
  VolumeX,
  X,
} from "lucide-react";
import { api } from "@/lib/api";
import type {
  AccountDetailResponse,
  RelationshipState,
} from "@/lib/types";

interface AccountInspectorDrawerProps {
  workspaceId: number;
  accountId: number | null;
  onClose: () => void;
  onAccountMutated: () => Promise<void> | void;
}

function formatRelState(state: RelationshipState): {
  label: string;
  badgeClass: string;
} {
  switch (state) {
    case "mutual_active":
      return {
        label: "Mutual",
        badgeClass: "border-emerald-500/40 bg-emerald-500/15 text-emerald-300",
      };
    case "inbound_unreciprocated":
      return {
        label: "Follows You · Follow Back",
        badgeClass: "border-sky-500/40 bg-sky-500/15 text-sky-300",
      };
    case "outbound_warming":
      return {
        label: "Warming Up",
        badgeClass: "border-amber-500/40 bg-amber-500/15 text-amber-300",
      };
    case "mutual_dormant":
      return {
        label: "Mutual (Quiet >14d)",
        badgeClass: "border-indigo-500/40 bg-indigo-500/15 text-indigo-300",
      };
    case "outbound_cold":
      return {
        label: "Following (One-Way)",
        badgeClass: "border-slate-700 bg-slate-800/80 text-slate-400",
      };
    default:
      return {
        label: "Tracked Voice",
        badgeClass: "border-slate-700 bg-slate-800/70 text-slate-400",
      };
  }
}

function compactNumber(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return String(n);
}

export function AccountInspectorDrawer({
  workspaceId,
  accountId,
  onClose,
  onAccountMutated,
}: AccountInspectorDrawerProps) {
  const [detail, setDetail] = useState<AccountDetailResponse | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [mutating, setMutating] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  const loadDetail = useCallback(async () => {
    if (accountId == null) return;
    setLoading(true);
    setError(null);
    try {
      const res = await api.getNetworkAccountDetail(workspaceId, accountId);
      setDetail(res);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load profile.");
    } finally {
      setLoading(false);
    }
  }, [workspaceId, accountId]);

  useEffect(() => {
    if (accountId != null) {
      void loadDetail();
    } else {
      setDetail(null);
    }
  }, [accountId, loadDetail]);

  if (accountId == null) return null;

  const handleToggleFollowedByMe = async () => {
    if (!detail || mutating) return;
    setMutating(true);
    setError(null);
    try {
      await api.updateNetworkAccount(workspaceId, detail.account.id, {
        followed_by_me: !detail.account.followed_by_me,
      });
      await loadDetail();
      await onAccountMutated();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to update follow status.");
    } finally {
      setMutating(false);
    }
  };

  const handleMuteAccount = async () => {
    if (!detail || mutating) return;
    setMutating(true);
    try {
      await api.updateNetworkAccount(workspaceId, detail.account.id, {
        is_muted: true,
      });
      onClose();
      await onAccountMutated();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to mute person.");
      setMutating(false);
    }
  };

  const handleDeleteAccount = async () => {
    if (!detail || mutating) return;
    setMutating(true);
    try {
      await api.deleteNetworkAccount(workspaceId, detail.account.id);
      onClose();
      await onAccountMutated();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to remove person.");
      setMutating(false);
    }
  };

  const acct = detail?.account;
  const relBadge = acct ? formatRelState(acct.relationship_state) : null;

  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-black/55 backdrop-blur-xs">
      <div
        className="flex h-full w-full max-w-lg flex-col border-l border-slate-800 bg-surface-900 shadow-2xl"
        role="dialog"
        aria-label="Person Details"
      >
        {/* Header */}
        <div className="flex items-start justify-between gap-3 border-b border-slate-800 px-5 py-4">
          {acct ? (
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2">
                <span
                  className="h-2.5 w-2.5 rounded-full shrink-0"
                  style={{
                    backgroundColor: acct.primary_community_color || "#38bdf8",
                  }}
                />
                <h2 className="truncate text-base font-semibold text-white">
                  {acct.display_name}
                </h2>
                <a
                  href={`https://x.com/${acct.handle}`}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-xs text-sky-400 hover:text-sky-300 hover:underline"
                  title={`Open @${acct.handle} on X`}
                >
                  @{acct.handle}
                </a>
                {acct.is_mutual_20x && (
                  <span className="rounded-full border border-emerald-500/40 bg-emerald-500/15 px-2 py-0.5 text-[10px] font-semibold text-emerald-300">
                    Mutual
                  </span>
                )}
                {acct.is_auto_tracked && (
                  <span className="rounded-full border border-cyan-500/40 bg-cyan-500/15 px-2 py-0.5 text-[10px] font-semibold text-cyan-300">
                    {acct.followers_count < 50_000
                      ? "Rising · Auto-Tracked"
                      : "Core Affinity · Auto-Tracked"}
                  </span>
                )}
              </div>
              <p className="mt-1 line-clamp-2 text-xs text-slate-400">
                {acct.bio || "Tracked X account"}
              </p>
              <div className="mt-2 flex flex-wrap items-center gap-3 text-[11px] text-slate-400">
                <span>
                  <strong className="text-slate-200">
                    {compactNumber(acct.followers_count)}
                  </strong>{" "}
                  followers
                </span>
                <span>
                  <strong className="text-slate-200">
                    {compactNumber(acct.following_count)}
                  </strong>{" "}
                  following
                </span>
                {typeof acct.yield_score === "number" && (
                  <span>
                    <strong className="text-violet-300">
                      {Math.round(acct.yield_score * 100)}%
                    </strong>{" "}
                    reply yield
                  </span>
                )}
                {typeof acct.core_affinity_score === "number" && (
                  <span>
                    <strong className="text-sky-300">
                      {Math.round(acct.core_affinity_score * 100)}%
                    </strong>{" "}
                    core affinity
                  </span>
                )}
              </div>
            </div>
          ) : (
            <div className="text-sm font-medium text-slate-300">Loading...</div>
          )}

          <div className="flex shrink-0 items-center gap-2">
            {acct && (
              <a
                href={acct.x_profile_url}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1.5 rounded-lg border border-sky-500/40 bg-sky-500/15 px-2.5 py-1.5 text-xs font-medium text-sky-300 transition hover:bg-sky-500/25"
              >
                Open on X
                <ExternalLink className="h-3.5 w-3.5" />
              </a>
            )}
            <button
              type="button"
              onClick={onClose}
              className="rounded-lg p-1.5 text-slate-400 transition hover:bg-slate-800 hover:text-white"
              aria-label="Close"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        </div>

        {/* Body */}
        <div className="flex-1 space-y-4 overflow-y-auto px-5 py-4">
          {error && (
            <div className="rounded-lg border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-xs text-rose-300">
              {error}
            </div>
          )}

          {loading && !detail ? (
            <div className="flex h-64 items-center justify-center gap-2 text-xs text-slate-400">
              <Loader2 className="h-4 w-4 animate-spin text-sky-400" />
              Loading profile...
            </div>
          ) : detail && acct ? (
            <>
              {/* Influence & Spark Summary */}
              <section className="grid grid-cols-2 gap-3">
                <div className="rounded-xl border border-slate-800 bg-surface-950/60 p-3.5">
                  <div className="flex items-center justify-between">
                    <span className="text-[11px] font-medium text-slate-400">
                      Influence Score
                    </span>
                    <Flame className="h-3.5 w-3.5 text-amber-400" />
                  </div>
                  <div className="mt-1 flex items-baseline gap-2">
                    <span className="text-2xl font-bold text-white">
                      {Math.round(acct.heavy_hitter_score)}
                    </span>
                    <span className="text-xs text-slate-500">/ 100</span>
                  </div>
                  <div className="mt-1.5 text-[11px] text-slate-400">
                    UserCredV2: {Math.round(acct.user_cred_v2)}/100
                  </div>
                </div>

                <div className="rounded-xl border border-slate-800 bg-surface-950/60 p-3.5">
                  <div className="flex items-center justify-between">
                    <span className="text-[11px] font-medium text-slate-400">
                      Spark Lift
                    </span>
                    <Sparkles className="h-3.5 w-3.5 text-sky-400" />
                  </div>
                  <div className="mt-1 flex items-baseline gap-1.5">
                    <span className="text-2xl font-bold text-amber-300">
                      {acct.catalysis_lift.toFixed(1)}x
                    </span>
                    <span className="text-xs text-slate-500">reach boost</span>
                  </div>
                  <div className="mt-1.5 text-[11px] text-slate-400">
                    {acct.catalyzed_posts_count} boosted conversation
                    {acct.catalyzed_posts_count === 1 ? "" : "s"}
                  </div>
                </div>
              </section>

              {(acct.reply_sample_count ?? 0) > 0 && (
                <section className="rounded-xl border border-violet-500/25 bg-violet-950/20 p-3.5">
                  <div className="flex items-center justify-between text-[11px] text-violet-200">
                    <span className="font-medium">
                      Observed Reply Yield ({acct.reply_sample_count}{" "}
                      {acct.reply_sample_count === 1 ? "reply" : "replies"})
                    </span>
                    <span className="font-mono text-violet-300">
                      ~{compactNumber(Math.round(acct.avg_reply_views ?? 0))} avg views
                      {(acct.attributed_follows_30d ?? 0) >= 0.1
                        ? ` · +${(acct.attributed_follows_30d ?? 0).toFixed(1)} follows (30d)`
                        : ""}
                    </span>
                  </div>
                </section>
              )}

              {/* Relationship Status */}
              {detail.mutual_history.has_own_handle && (
                <section className="rounded-xl border border-slate-800 bg-surface-950/60 p-4">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div>
                      <div className="text-[11px] font-medium text-slate-400">
                        Your Connection
                      </div>
                      {relBadge && (
                        <span
                          className={`mt-1 inline-block rounded-lg border px-2 py-0.5 text-xs font-medium ${relBadge.badgeClass}`}
                        >
                          {relBadge.label}
                        </span>
                      )}
                    </div>

                    <button
                      type="button"
                      disabled={mutating}
                      onClick={() => void handleToggleFollowedByMe()}
                      className={`inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-xs font-medium transition ${
                        acct.followed_by_me
                          ? "border-emerald-500/40 bg-emerald-500/15 text-emerald-300 hover:bg-emerald-500/25"
                          : "border-sky-500/40 bg-sky-500/15 text-sky-300 hover:bg-sky-500/25"
                      }`}
                    >
                      {acct.followed_by_me ? (
                        <>
                          <UserCheck className="h-3.5 w-3.5" />
                          Following
                        </>
                      ) : (
                        <>
                          <UserPlus className="h-3.5 w-3.5" />
                          Mark Followed
                        </>
                      )}
                    </button>
                  </div>

                  {detail.mutual_history.recommended_play && (
                    <p className="mt-2.5 text-xs leading-relaxed text-slate-300">
                      {detail.mutual_history.recommended_play}
                    </p>
                  )}
                </section>
              )}

              {/* Boosted Conversations */}
              {detail.catalyzed_conversations.length > 0 && (
                <section className="rounded-xl border border-slate-800 bg-surface-950/60 p-4">
                  <h3 className="text-xs font-semibold text-slate-200">
                    Recent Threads They Boosted ({detail.catalyzed_conversations.length})
                  </h3>
                  <div className="mt-2.5 space-y-2">
                    {detail.catalyzed_conversations.slice(0, 5).map((conv) => (
                      <div
                        key={conv.post_id}
                        className="rounded-lg border border-slate-800/90 bg-surface-900/80 p-2.5"
                      >
                        <div className="flex items-center justify-between gap-2 text-[11px]">
                          <span className="font-medium text-slate-200">
                            @{conv.author_handle}{" "}
                            <span className="text-slate-500">
                              via {conv.interaction_type}
                            </span>
                          </span>
                          <div className="flex items-center gap-2">
                            <span className="rounded bg-amber-500/15 px-1.5 py-0.5 text-[10px] font-semibold text-amber-300">
                              {conv.observed_lift.toFixed(1)}x boost
                            </span>
                            <a
                              href={conv.x_post_url}
                              target="_blank"
                              rel="noreferrer"
                              className="text-sky-400 hover:text-sky-300"
                            >
                              <ExternalLink className="h-3 w-3" />
                            </a>
                          </div>
                        </div>
                        <p className="mt-1 line-clamp-2 text-xs text-slate-300">
                          {conv.text}
                        </p>
                      </div>
                    ))}
                  </div>
                </section>
              )}

              {/* Footer Actions: Mute / Remove */}
              <div className="flex items-center justify-end gap-2 border-t border-slate-800 pt-3">
                <button
                  type="button"
                  disabled={mutating}
                  onClick={() => void handleMuteAccount()}
                  className="inline-flex items-center gap-1.5 rounded-lg border border-slate-700 bg-surface-850 px-2.5 py-1.5 text-xs font-medium text-slate-300 transition hover:bg-slate-800"
                  title="Never re-suggest or auto-track this person"
                >
                  <VolumeX className="h-3.5 w-3.5" />
                  Mute Forever
                </button>
                <button
                  type="button"
                  disabled={mutating}
                  onClick={() => void handleDeleteAccount()}
                  className="inline-flex items-center gap-1.5 rounded-lg border border-rose-500/30 bg-rose-500/10 px-2.5 py-1.5 text-xs font-medium text-rose-300 transition hover:bg-rose-500/20"
                >
                  <Trash2 className="h-3.5 w-3.5" />
                  Remove
                </button>
              </div>
            </>
          ) : null}
        </div>
      </div>
    </div>
  );
}
