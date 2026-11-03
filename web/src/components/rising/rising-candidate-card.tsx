"use client";

import React, { useMemo, useState } from "react";
import {
  Award,
  Check,
  ChevronDown,
  ChevronUp,
  ExternalLink,
  Plus,
  RotateCcw,
  TrendingUp,
  UserCheck,
  XCircle,
} from "lucide-react";
import type { Community } from "@/lib/types";
import type { RisingCandidate } from "@/lib/rising-types";

interface RisingCandidateCardProps {
  candidate: RisingCandidate;
  communities: Community[];
  onPromote: (candidateId: number, communityIds: number[]) => Promise<void>;
  onDismiss: (candidateId: number, restore: boolean) => Promise<void>;
  busyAction: boolean;
}

function formatEndorsementVerb(type: string): string {
  switch (type) {
    case "follow":
      return "followed";
    case "quote":
      return "quoted";
    case "reply":
      return "replied";
    default:
      return type;
  }
}

function endorsementBadgeStyle(type: string): string {
  switch (type) {
    case "follow":
      return "border-emerald-500/40 bg-emerald-500/10 text-emerald-200 hover:bg-emerald-500/20";
    case "quote":
      return "border-sky-500/40 bg-sky-500/10 text-sky-200 hover:bg-sky-500/20";
    case "reply":
    default:
      return "border-violet-500/40 bg-violet-500/10 text-violet-200 hover:bg-violet-500/20";
  }
}

export function RisingCandidateCard({
  candidate,
  communities,
  onPromote,
  onDismiss,
  busyAction,
}: RisingCandidateCardProps) {
  const defaultSelectedIds = useMemo(() => {
    const projectedIds = candidate.community_projection
      .filter((p) => p.weight >= 0.25)
      .map((p) => p.community_id);
    if (projectedIds.length > 0) return projectedIds;
    if (candidate.community_projection.length > 0) {
      return [candidate.community_projection[0].community_id];
    }
    return communities[0] ? [communities[0].id] : [];
  }, [candidate.community_projection, communities]);

  const [promoteOpen, setPromoteOpen] = useState<boolean>(false);
  const [selectedCommIds, setSelectedCommIds] = useState<number[]>(defaultSelectedIds);
  const [postsExpanded, setPostsExpanded] = useState<boolean>(false);

  function toggleCommunityId(cid: number) {
    setSelectedCommIds((prev) =>
      prev.includes(cid) ? prev.filter((id) => id !== cid) : [...prev, cid]
    );
  }

  async function handleConfirmPromote() {
    const ids = selectedCommIds.length > 0 ? selectedCommIds : defaultSelectedIds;
    await onPromote(candidate.id, ids);
    setPromoteOpen(false);
  }

  const growthPct = Math.round(candidate.followers_growth_rate * 100);

  return (
    <article className="rounded-2xl border border-slate-800 bg-surface-900/85 hover:border-slate-700/90 transition-colors p-5 space-y-4">
      {/* Top Row: Identity + Core 3 Scores */}
      <div className="flex flex-col lg:flex-row lg:items-start justify-between gap-4">
        <div className="space-y-1.5 min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <a
              href={candidate.profile_url}
              target="_blank"
              rel="noreferrer"
              className="text-base font-semibold text-white hover:text-sky-300 inline-flex items-center gap-1.5"
            >
              <span>{candidate.display_name || `@${candidate.handle}`}</span>
              <span className="text-xs font-mono font-normal text-sky-400">
                @{candidate.handle}
              </span>
              <ExternalLink className="h-3.5 w-3.5 text-slate-400" />
            </a>

            <span className="px-2 py-0.5 rounded-full bg-surface-850 border border-slate-700 text-xs font-mono text-slate-200">
              {candidate.followers_count.toLocaleString()} followers
            </span>

            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-emerald-500/10 border border-emerald-500/30 text-emerald-300 text-[11px] font-medium">
              <TrendingUp className="h-3 w-3" />
              <span>+{growthPct}% 7d</span>
            </span>

            {candidate.status !== "active" && (
              <span
                className={`px-2 py-0.5 rounded-full text-[11px] font-semibold uppercase ${
                  candidate.status === "promoted"
                    ? "bg-sky-500/15 text-sky-300 border border-sky-500/30"
                    : "bg-slate-800 text-slate-400 border border-slate-700"
                }`}
              >
                {candidate.status}
              </span>
            )}
          </div>

          <p className="text-xs text-slate-300 leading-relaxed">{candidate.bio}</p>
        </div>

        {/* Score Trio */}
        <div className="grid grid-cols-3 gap-2.5 shrink-0">
          <div className="rounded-xl bg-amber-500/10 border border-amber-500/30 px-3 py-2 text-center min-w-[96px]">
            <div className="text-[10px] uppercase tracking-wider text-amber-300/90 font-semibold">
              Combined
            </div>
            <div className="text-xl font-mono font-bold text-amber-300 mt-0.5">
              {candidate.combined_rising_score.toFixed(1)}
            </div>
          </div>

          <div className="rounded-xl bg-surface-850 border border-slate-800 px-3 py-2 text-center min-w-[112px]">
            <div className="text-[10px] uppercase tracking-wider text-slate-400 font-semibold">
              Punch-Weight
            </div>
            <div className="text-base font-mono font-semibold text-sky-300 mt-0.5">
              {candidate.punch_above_weight_score.toFixed(1)}
            </div>
            <div className="text-[10px] text-slate-400 font-mono">
              {candidate.posts_above_3x_count}/{candidate.sampled_posts_count} &gt;3x ·{" "}
              {candidate.median_reach_multiple.toFixed(1)}x
            </div>
          </div>

          <div className="rounded-xl bg-surface-850 border border-slate-800 px-3 py-2 text-center min-w-[104px]">
            <div className="text-[10px] uppercase tracking-wider text-slate-400 font-semibold">
              Peer Respect
            </div>
            <div className="text-base font-mono font-semibold text-emerald-300 mt-0.5">
              {candidate.peer_respect_score.toFixed(1)}
            </div>
            <div className="text-[10px] text-slate-400 font-mono">
              {candidate.distinct_endorsers_count} peer
              {candidate.distinct_endorsers_count === 1 ? "" : "s"}
            </div>
          </div>
        </div>
      </div>

      {/* Stacked K-Community Affinity Projection Bar */}
      {candidate.community_projection.length > 0 && (
        <div className="space-y-1.5">
          <div className="flex items-center justify-between text-[11px] text-slate-400">
            <span>K-Community SimClusters InterestedIn Projection</span>
            <div className="flex flex-wrap items-center gap-3">
              {candidate.community_projection.map((slice) => (
                <span
                  key={slice.community_id}
                  className="inline-flex items-center gap-1.5 text-slate-300"
                >
                  <span
                    className="h-2 w-2 rounded-full"
                    style={{ backgroundColor: slice.color }}
                  />
                  <span>{slice.name}</span>
                  <span className="font-mono text-slate-400">
                    {Math.round(slice.weight * 100)}%
                  </span>
                </span>
              ))}
            </div>
          </div>

          <div className="h-2.5 w-full rounded-full bg-surface-950 overflow-hidden flex border border-slate-800/80">
            {candidate.community_projection.map((slice) => (
              <div
                key={slice.community_id}
                style={{
                  width: `${Math.max(4, slice.weight * 100)}%`,
                  backgroundColor: slice.color,
                }}
                title={`${slice.name}: ${(slice.weight * 100).toFixed(1)}%`}
                className="h-full first:rounded-l-full last:rounded-r-full transition-all"
              />
            ))}
          </div>
        </div>
      )}

      {/* Clickable Peer Co-Sign Badges */}
      <div className="space-y-1.5">
        <div className="text-[11px] font-medium text-slate-400 flex items-center gap-1.5">
          <Award className="h-3.5 w-3.5 text-amber-400" />
          <span>Tracked Heavy-Hitter &amp; SALSA Scout Co-Signs</span>
        </div>

        {candidate.endorsements.length === 0 ? (
          <div className="text-xs text-slate-500">
            No tracked peer endorsements recorded yet. Click Refresh Discovery to hydrate 2-hop co-signs.
          </div>
        ) : (
          <div className="flex flex-wrap gap-2">
            {candidate.endorsements.map((end) => (
              <a
                key={end.id}
                href={end.interaction_url}
                target="_blank"
                rel="noreferrer"
                title={`${end.display_name} · Heavy Hitter ${end.heavy_hitter_score.toFixed(1)} · SALSA Scout ${end.salsa_scout.toFixed(2)} · Weight +${end.weight_contribution.toFixed(2)}`}
                className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg border text-xs transition-colors ${endorsementBadgeStyle(
                  end.endorsement_type
                )}`}
              >
                <span className="font-mono font-medium">@{end.handle}</span>
                <span className="opacity-90">
                  {formatEndorsementVerb(end.endorsement_type)} (
                  {end.signal_multiplier.toFixed(0)}x)
                </span>
                <span className="text-[10px] opacity-75 font-mono">
                  HH {Math.round(end.heavy_hitter_score)}
                </span>
                <ExternalLink className="h-3 w-3 opacity-70" />
              </a>
            ))}
          </div>
        )}
      </div>

      {/* Footer Actions + Expandable Outlier Posts */}
      <div className="pt-2 border-t border-slate-800/80 flex flex-wrap items-center justify-between gap-3 relative">
        <div className="flex items-center gap-3">
          {candidate.top_posts_json.length > 0 && (
            <button
              type="button"
              onClick={() => setPostsExpanded((p) => !p)}
              className="inline-flex items-center gap-1.5 text-xs text-sky-400 hover:text-sky-300 font-medium"
            >
              {postsExpanded ? (
                <ChevronUp className="h-3.5 w-3.5" />
              ) : (
                <ChevronDown className="h-3.5 w-3.5" />
              )}
              <span>
                {postsExpanded ? "Hide" : "Preview"} Outlier Post
                {candidate.top_posts_json.length > 1 ? "s" : ""} (
                {candidate.top_posts_json.length})
              </span>
            </button>
          )}

          <span className="text-[11px] text-slate-400">
            {candidate.meets_consistency_gate
              ? "✓ Multi-post breakout consistency verified (≥3 of ≥5 posts >3.0x)"
              : "⚠ Below 3-of-5 consistency floor (one-hit-wonder discount applied)"}
          </span>
        </div>

        <div className="flex items-center gap-2">
          {candidate.status === "active" && (
            <>
              <div className="relative">
                <button
                  type="button"
                  disabled={busyAction}
                  onClick={() => setPromoteOpen((o) => !o)}
                  className="inline-flex items-center gap-1.5 px-3.5 py-1.5 rounded-xl bg-sky-500 hover:bg-sky-400 text-slate-950 text-xs font-semibold transition-colors disabled:opacity-50"
                >
                  <Plus className="h-3.5 w-3.5" />
                  <span>Add to Community</span>
                </button>

                {promoteOpen && (
                  <div className="absolute right-0 bottom-10 w-72 rounded-xl bg-surface-900 border border-slate-700 shadow-2xl p-3.5 z-20 space-y-3">
                    <div className="text-xs font-semibold text-white">
                      Promote @{candidate.handle} to Communities
                    </div>
                    <div className="space-y-1.5 max-h-44 overflow-y-auto">
                      {communities.map((c) => {
                        const checked = selectedCommIds.includes(c.id);
                        return (
                          <button
                            key={c.id}
                            type="button"
                            onClick={() => toggleCommunityId(c.id)}
                            className={`w-full flex items-center justify-between px-2.5 py-1.5 rounded-lg text-xs border transition-colors ${
                              checked
                                ? "border-sky-500/50 bg-sky-500/15 text-white"
                                : "border-slate-800 bg-surface-850 text-slate-300 hover:bg-surface-800"
                            }`}
                          >
                            <span className="flex items-center gap-2 truncate">
                              <span
                                className="h-2.5 w-2.5 rounded-full shrink-0"
                                style={{ backgroundColor: c.color }}
                              />
                              <span className="truncate">{c.name}</span>
                            </span>
                            {checked && <Check className="h-3.5 w-3.5 text-sky-400 shrink-0" />}
                          </button>
                        );
                      })}
                    </div>
                    <div className="flex justify-end gap-2 pt-1">
                      <button
                        type="button"
                        onClick={() => setPromoteOpen(false)}
                        className="px-2.5 py-1 rounded-lg border border-slate-700 text-xs text-slate-300 hover:bg-surface-800"
                      >
                        Cancel
                      </button>
                      <button
                        type="button"
                        disabled={busyAction || (selectedCommIds.length === 0 && communities.length > 0)}
                        onClick={() => void handleConfirmPromote()}
                        className="inline-flex items-center gap-1 px-3 py-1 rounded-lg bg-emerald-500 hover:bg-emerald-400 text-slate-950 text-xs font-semibold disabled:opacity-50"
                      >
                        <UserCheck className="h-3.5 w-3.5" />
                        <span>1-Click Promote</span>
                      </button>
                    </div>
                  </div>
                )}
              </div>

              <button
                type="button"
                disabled={busyAction}
                onClick={() => void onDismiss(candidate.id, false)}
                className="inline-flex items-center gap-1 px-3 py-1.5 rounded-xl border border-slate-700 bg-surface-850 hover:bg-rose-500/15 hover:border-rose-500/40 hover:text-rose-200 text-slate-300 text-xs font-medium transition-colors disabled:opacity-50"
              >
                <XCircle className="h-3.5 w-3.5" />
                <span>Dismiss</span>
              </button>
            </>
          )}

          {candidate.status === "dismissed" && (
            <button
              type="button"
              disabled={busyAction}
              onClick={() => void onDismiss(candidate.id, true)}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-xl border border-emerald-500/40 bg-emerald-500/10 hover:bg-emerald-500/20 text-emerald-200 text-xs font-medium transition-colors disabled:opacity-50"
            >
              <RotateCcw className="h-3.5 w-3.5" />
              <span>Restore to Active</span>
            </button>
          )}
        </div>
      </div>

      {/* Expandable Sample High-Reach Posts */}
      {postsExpanded && candidate.top_posts_json.length > 0 && (
        <div className="space-y-2 pt-1">
          {candidate.top_posts_json.map((post) => (
            <div
              key={post.x_post_id}
              className="rounded-xl bg-surface-950/80 border border-slate-800 p-3.5 flex flex-col sm:flex-row sm:items-center justify-between gap-3"
            >
              <p className="text-xs text-slate-200 leading-relaxed flex-1">{post.text}</p>
              <div className="flex items-center gap-2.5 shrink-0">
                <span className="px-2 py-0.5 rounded bg-amber-500/15 border border-amber-500/30 text-amber-300 font-mono text-xs font-semibold">
                  {post.reach_multiple.toFixed(1)}x reach
                </span>
                <span className="text-xs font-mono text-slate-400">
                  {post.views.toLocaleString()} views
                </span>
                <a
                  href={post.post_url}
                  target="_blank"
                  rel="noreferrer"
                  className="text-xs text-sky-400 hover:underline inline-flex items-center gap-1"
                >
                  <span>View</span>
                  <ExternalLink className="h-3 w-3" />
                </a>
              </div>
            </div>
          ))}
        </div>
      )}
    </article>
  );
}
