"use client";

import React from "react";
import {
  Bookmark,
  BookmarkMinus,
  BookmarkPlus,
  Check,
  ChevronDown,
  ChevronUp,
  ExternalLink,
  Flame,
  Heart,
  MessageCircle,
  Quote,
  Repeat2,
  Sparkles,
  TrendingUp,
  Zap,
} from "lucide-react";
import type { ActionKind, QueueStatus, RadarPostCard } from "@/lib/radar-types";
import { HoverTip } from "@/components/ui/hover-tip";
import { TrajectoryDrawer } from "./trajectory-drawer";

interface BreakoutPostCardProps {
  workspaceId: number;
  post: RadarPostCard;
  expanded: boolean;
  onToggleExpand: () => void;
  onQueueAction?: (
    post: RadarPostCard,
    status: QueueStatus,
    actionKind?: ActionKind
  ) => void;
  /** Card is rendered in the Saved tab: offers Unsave instead of Save / Skip. */
  savedView?: boolean;
}

function formatCompact(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return String(Math.round(n));
}

function formatAge(minutes: number): string {
  if (minutes < 60) return `${Math.max(1, Math.round(minutes))}m`;
  const hrs = minutes / 60;
  return hrs < 10 ? `${hrs.toFixed(1)}h` : `${Math.round(hrs)}h`;
}

export function BreakoutPostCard({
  workspaceId,
  post,
  expanded,
  onToggleExpand,
  onQueueAction,
  savedView = false,
}: BreakoutPostCardProps) {
  const isSurge = post.alert_tier === "surge";
  const isMutual = post.author.is_mutual;
  const scoreInt = Math.round(post.breakout_score);

  const paceLabel =
    post.hover_summary?.pace_label ||
    `${post.velocity_multiple.toFixed(1)}x usual views at ${formatAge(post.age_minutes)}`;
  const typicalViews =
    post.hover_summary?.typical_views ?? Math.max(1, Math.round(post.expected_p50_views));
  const whyReasons = post.hover_summary?.why_reasons?.length
    ? post.hover_summary.why_reasons
    : [
        `${post.latest_views.toLocaleString()} views in ${formatAge(post.age_minutes)} vs ~${typicalViews.toLocaleString()} typical`,
        post.tactical_rationale,
      ];

  const primaryIsReply = isMutual || post.recommended_action === "reply_mutual_20x";
  const primaryIntentUrl = primaryIsReply
    ? post.x_links.reply_intent_url
    : post.x_links.quote_intent_url;

  const hoverDetails = (
    <div className="space-y-2">
      <div className="flex items-center justify-between gap-3 border-b border-slate-800 pb-1.5">
        <span className="font-semibold text-white">{paceLabel}</span>
        <span className="font-mono text-[11px] text-emerald-300">
          Score {scoreInt}/100
        </span>
      </div>
      <div className="space-y-1 text-[11px] text-slate-300">
        {whyReasons.map((reason, idx) => (
          <div key={idx} className="flex items-start gap-1.5">
            <span className="text-sky-400 mt-0.5">•</span>
            <span>{reason}</span>
          </div>
        ))}
      </div>
      <div className="pt-1 border-t border-slate-800/80 flex items-center justify-between text-[11px] text-slate-400">
        <span>@{post.author.handle}</span>
        <span>{formatCompact(post.author.followers_count)} followers</span>
      </div>
    </div>
  );

  function handlePrimaryClick(e: React.MouseEvent) {
    e.stopPropagation();
    if (typeof window !== "undefined") {
      window.open(primaryIntentUrl, "_blank", "noopener,noreferrer");
    }
    onQueueAction?.(post, "acted", primaryIsReply ? "reply" : "quote");
  }

  function handleSecondaryIntent(
    e: React.MouseEvent,
    url: string,
    kind: ActionKind
  ) {
    e.stopPropagation();
    if (typeof window !== "undefined") {
      window.open(url, "_blank", "noopener,noreferrer");
    }
    onQueueAction?.(post, "acted", kind);
  }

  return (
    <article
      className={`rounded-2xl border transition-all p-4 sm:p-5 ${
        isSurge
          ? "bg-gradient-to-br from-surface-900 via-surface-900 to-amber-950/15 border-amber-500/35 hover:border-amber-400/55"
          : "bg-surface-900/90 border-slate-800/90 hover:border-slate-700"
      }`}
    >
      <div className="flex flex-col sm:flex-row sm:items-start justify-between gap-4">
        {/* Left: Author + Post + Network Engagement */}
        <div className="min-w-0 flex-1 space-y-2.5">
          {/* Header Row */}
          <div className="flex items-center flex-wrap gap-2">
            <a
              href={`https://x.com/${post.author.handle}`}
              target="_blank"
              rel="noreferrer"
              className="font-semibold text-sm text-white hover:text-sky-300 transition-colors"
            >
              {post.author.display_name}
            </a>
            <span className="text-xs text-slate-400">@{post.author.handle}</span>
            <span className="text-slate-600">·</span>
            <a
              href={post.x_links.post_url}
              target="_blank"
              rel="noreferrer"
              className="text-xs font-medium text-slate-400 hover:text-slate-200 inline-flex items-center gap-1"
              title="Open original post on X"
            >
              <span>{formatAge(post.age_minutes)}</span>
              <ExternalLink className="h-3 w-3 opacity-70" />
            </a>

            {isMutual && (
              <span className="inline-flex items-center px-2 py-0.5 rounded-full text-[11px] font-medium bg-emerald-500/15 border border-emerald-500/35 text-emerald-300">
                Mutual
              </span>
            )}

            {post.author.is_auto_tracked && (
              <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] font-medium bg-cyan-500/15 border border-cyan-500/35 text-cyan-300">
                <Sparkles className="h-2.5 w-2.5" />
                Rising
              </span>
            )}

            {(post.author.reply_sample_count ?? 0) > 0 &&
              (post.author.avg_reply_views ?? 0) >= 100 && (
                <span
                  className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] font-medium bg-violet-500/15 border border-violet-500/35 text-violet-200"
                  title={`Proven reply yield across ${post.author.reply_sample_count} ${
                    post.author.reply_sample_count === 1 ? "reply" : "replies"
                  }`}
                >
                  ~{formatCompact(post.author.avg_reply_views ?? 0)} reply views
                </span>
              )}
          </div>

          {/* Post Text */}
          <p className="text-[15px] text-slate-100 leading-relaxed whitespace-pre-line">
            {post.text}
          </p>

          {/* Network Voices Callout */}
          {post.interactions.length > 0 && (
            <div className="inline-flex items-center gap-1.5 text-xs text-amber-200/90 bg-amber-500/10 border border-amber-500/20 rounded-lg px-2.5 py-1">
              <Zap className="h-3.5 w-3.5 text-amber-400 shrink-0" />
              <span>
                {post.interactions
                  .slice(0, 3)
                  .map((it) => `@${it.actor_handle}`)
                  .join(", ")}{" "}
                engaged early
              </span>
            </div>
          )}

          {/* Engagement Bar + Curve Toggle */}
          <div className="pt-1 flex flex-wrap items-center justify-between gap-3 text-xs text-slate-400">
            <div className="flex flex-wrap items-center gap-3.5">
              <span className="text-slate-300 font-medium">
                {formatCompact(post.latest_views)} views
              </span>
              <span className="inline-flex items-center gap-1">
                <MessageCircle className="h-3.5 w-3.5 text-slate-500" />
                {formatCompact(post.latest_replies)}
              </span>
              <span className="inline-flex items-center gap-1">
                <Quote className="h-3.5 w-3.5 text-slate-500" />
                {formatCompact(post.latest_quotes)}
              </span>
              <span className="inline-flex items-center gap-1">
                <Repeat2 className="h-3.5 w-3.5 text-slate-500" />
                {formatCompact(post.latest_reposts)}
              </span>
              <span className="inline-flex items-center gap-1">
                <Bookmark className="h-3.5 w-3.5 text-slate-500" />
                {formatCompact(post.latest_bookmarks)}
              </span>
              <span className="inline-flex items-center gap-1">
                <Heart className="h-3.5 w-3.5 text-slate-500" />
                {formatCompact(post.latest_likes)}
              </span>
            </div>

            <button
              type="button"
              onClick={onToggleExpand}
              className="inline-flex items-center gap-1 text-xs font-medium text-slate-400 hover:text-sky-300 transition-colors"
            >
              <span>{expanded ? "Hide curve" : "View curve"}</span>
              {expanded ? (
                <ChevronUp className="h-3.5 w-3.5" />
              ) : (
                <ChevronDown className="h-3.5 w-3.5" />
              )}
            </button>
          </div>
        </div>

        {/* Right: Momentum Score (with Instant Hover Tooltip) + Clean Actions */}
        <div className="flex sm:flex-col items-center sm:items-end justify-between sm:justify-start gap-3 shrink-0 sm:min-w-[148px] pt-2 sm:pt-0 border-t sm:border-t-0 border-slate-800/80">
          <HoverTip content={hoverDetails} side="left" align="start" maxWidthClass="max-w-sm">
            <button
              type="button"
              onClick={onToggleExpand}
              className="flex items-center gap-2.5 rounded-xl px-3 py-1.5 bg-surface-950/90 border border-slate-800 hover:border-slate-700 transition-colors text-left"
            >
              <div>
                <div className="flex items-center gap-1.5">
                  {isSurge ? (
                    <span className="inline-flex items-center gap-1 text-[11px] font-semibold text-amber-300">
                      <Flame className="h-3.5 w-3.5 text-amber-400" />
                      Surging
                    </span>
                  ) : (
                    <span className="inline-flex items-center gap-1 text-[11px] font-semibold text-sky-300">
                      <TrendingUp className="h-3.5 w-3.5 text-sky-400" />
                      Heating Up
                    </span>
                  )}
                </div>
                <div className="text-[11px] text-slate-400">
                  {post.velocity_multiple.toFixed(1)}x usual
                </div>
              </div>
              <div
                className={`text-2xl font-bold tabular-nums leading-none pl-2 border-l border-slate-800 ${
                  isSurge ? "text-amber-300" : "text-sky-300"
                }`}
              >
                {scoreInt}
              </div>
            </button>
          </HoverTip>

          <div className="flex items-center gap-1.5">
            <button
              type="button"
              onClick={handlePrimaryClick}
              className={`inline-flex items-center gap-1.5 px-3.5 py-2 rounded-xl text-xs font-semibold transition-colors shadow-sm ${
                primaryIsReply
                  ? "bg-emerald-500 hover:bg-emerald-400 text-slate-950"
                  : "bg-sky-500 hover:bg-sky-400 text-slate-950"
              }`}
            >
              {primaryIsReply ? (
                <>
                  <MessageCircle className="h-3.5 w-3.5" />
                  <span>Reply</span>
                </>
              ) : (
                <>
                  <Quote className="h-3.5 w-3.5" />
                  <span>Quote</span>
                </>
              )}
            </button>

            <button
              type="button"
              onClick={(e) =>
                handleSecondaryIntent(
                  e,
                  primaryIsReply
                    ? post.x_links.quote_intent_url
                    : post.x_links.reply_intent_url,
                  primaryIsReply ? "quote" : "reply"
                )
              }
              className="px-2.5 py-2 rounded-xl bg-surface-850 hover:bg-surface-800 border border-slate-800 text-xs font-medium text-slate-300 transition-colors"
              title={primaryIsReply ? "Quote instead" : "Reply instead"}
            >
              {primaryIsReply ? "Quote" : "Reply"}
            </button>

            {onQueueAction && savedView && (
              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation();
                  onQueueAction(post, "open");
                }}
                className="inline-flex items-center gap-1 px-2.5 py-2 rounded-xl hover:bg-surface-850 text-xs font-medium text-slate-400 hover:text-slate-200 transition-colors"
                title="Remove from Saved"
              >
                <BookmarkMinus className="h-3.5 w-3.5" />
                <span>Unsave</span>
              </button>
            )}

            {onQueueAction && !savedView && (
              <>
                <button
                  type="button"
                  onClick={(e) => {
                    e.stopPropagation();
                    onQueueAction(post, "saved");
                  }}
                  className="inline-flex items-center gap-1 px-2.5 py-2 rounded-xl hover:bg-surface-850 text-xs font-medium text-slate-400 hover:text-amber-200 transition-colors"
                  title="Save for later and clear from queue"
                >
                  <BookmarkPlus className="h-3.5 w-3.5" />
                  <span>Save</span>
                </button>
                <button
                  type="button"
                  onClick={(e) => {
                    e.stopPropagation();
                    onQueueAction(post, "skipped");
                  }}
                  className="inline-flex items-center gap-1 px-2.5 py-2 rounded-xl hover:bg-surface-850 text-xs font-medium text-slate-400 hover:text-slate-200 transition-colors"
                  title="Clear from queue"
                >
                  <Check className="h-3.5 w-3.5" />
                  <span>Skip</span>
                </button>
              </>
            )}
          </div>
        </div>
      </div>

      {/* Expandable Growth Curve */}
      {expanded && (
        <TrajectoryDrawer
          workspaceId={workspaceId}
          postId={post.id}
          onClose={onToggleExpand}
        />
      )}
    </article>
  );
}
