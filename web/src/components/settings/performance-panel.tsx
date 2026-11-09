"use client";

import type { ReactNode } from "react";
import { Info } from "lucide-react";
import { HoverTip } from "@/components/ui/hover-tip";
import type { PerformanceResponse } from "@/lib/backtest-types";

const fmtInt = (n: number | null | undefined) =>
  n === null || n === undefined ? "—" : Math.round(n).toLocaleString();

const fmtSigned = (n: number | null | undefined) =>
  n === null || n === undefined ? "—" : `${n > 0 ? "+" : ""}${Math.round(n).toLocaleString()}`;

const fmtPct = (share: number | null | undefined) =>
  share === null || share === undefined ? "—" : `${Math.round(share * 100)}%`;

const fmtMult = (m: number | null | undefined) =>
  m === null || m === undefined ? "—" : `${m.toFixed(1)}x`;

function MetricCard({
  label,
  value,
  sub,
  tip,
  valueClass = "text-white",
}: {
  label: string;
  value: string;
  sub: string;
  tip: ReactNode;
  valueClass?: string;
}) {
  return (
    <div className="rounded-xl bg-surface-950/80 border border-slate-800 p-3.5">
      <HoverTip content={tip} maxWidthClass="max-w-sm">
        <button
          type="button"
          className="inline-flex items-center gap-1 text-[11px] text-slate-400 hover:text-slate-200 cursor-help"
        >
          <span>{label}</span>
          <Info className="h-3 w-3" />
        </button>
      </HoverTip>
      <div className={`text-xl font-bold mt-0.5 ${valueClass}`}>{value}</div>
      <div className="text-[11px] text-slate-500 mt-0.5">{sub}</div>
    </div>
  );
}

function Tip({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="space-y-1.5 leading-relaxed">
      <div className="font-semibold text-white">{title}</div>
      {children}
    </div>
  );
}

export function PerformancePanel({ perf }: { perf: PerformanceResponse }) {
  const s = perf.success;
  const a = perf.accuracy;
  const days = s.window_days;

  return (
    <div className="space-y-4">
      <div>
        <div className="text-xs font-semibold text-slate-200 mb-2">
          Growth · last {days} days
        </div>
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
          <MetricCard
            label="Net new followers"
            value={fmtSigned(s.net_new_followers)}
            valueClass={
              (s.net_new_followers ?? 0) > 0 ? "text-emerald-300" : "text-white"
            }
            sub={`Previous ${days} days: ${fmtSigned(s.prev_net_new_followers)}`}
            tip={
              <Tip title="Net new followers">
                <p>
                  Your follower count now minus your follower count {days} days ago. Unfollows are
                  included, so this is the real change, not just new follows.
                </p>
                <p>
                  How it's measured: TweetTracker reads your profile's follower count each network
                  sync (about every 6 hours), so the start and end can be off by a few hours.
                </p>
                <p>
                  Check it in X: Analytics → Account overview → &quot;Follows over time&quot;
                  (follows minus unfollows for the same days).
                </p>
                <p>This is the main measure of whether the account is growing.</p>
              </Tip>
            }
          />
          <MetricCard
            label="Reply impressions"
            value={fmtInt(s.reply_impressions)}
            valueClass="text-sky-300"
            sub={`Previous ${days} days: ${fmtInt(s.prev_reply_impressions)}`}
            tip={
              <Tip title="Reply impressions">
                <p>
                  Total views on the replies and quotes you wrote in the last {days} days, added up.
                  Only posts authored by you count; posts you retweeted are excluded.
                </p>
                <p>
                  How it's measured: the view count X reports on each of your replies and quotes,
                  refreshed until the post is 24–48 hours old, so recent replies keep growing.
                </p>
                <p>
                  Check it in X: Analytics → Impressions. X&apos;s total also includes your
                  original posts, so it will be higher than this number.
                </p>
                <p>This is the reach your replying created.</p>
              </Tip>
            }
          />
          <MetricCard
            label="Views per reply"
            value={fmtInt(s.median_views_per_reply)}
            sub={`Previous ${days} days: ${fmtInt(s.prev_median_views_per_reply)}`}
            tip={
              <Tip title="Views per reply (median)">
                <p>
                  The typical number of views one of your replies or quotes gets: half get more,
                  half get fewer. A median is used so one lucky reply doesn&apos;t hide the rest.
                </p>
                <p>
                  What moves it: replying earlier, to bigger posts, and to accounts that follow you
                  back (X weights replies between mutuals much higher).
                </p>
                <p>This is the quality of your replies; Reply impressions is quality × volume.</p>
              </Tip>
            }
          />
          <MetricCard
            label="Replies sent"
            value={fmtInt(s.replies_sent)}
            sub={`Previous ${days} days: ${fmtInt(s.prev_replies_sent)}`}
            tip={
              <Tip title="Replies sent">
                <p>
                  How many replies and quotes you posted in the last {days} days, from Hot Posts or
                  anywhere else on X.
                </p>
                <p>
                  This is the input you control. Reach and followers only move when replies go
                  out, and the reply-yield scores that rank Hot Posts only learn from replies you
                  actually send.
                </p>
              </Tip>
            }
          />
        </div>
      </div>

      <div>
        <div className="text-xs font-semibold text-slate-200 mb-2">
          Hot Posts accuracy · posts from the last {a.window_days} days
        </div>
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
          <MetricCard
            label="Reach forecast"
            value={fmtMult(a.median_actual_vs_projected)}
            sub={
              a.graded_posts > 0
                ? `${fmtPct(a.share_within_2x)} within 2x · ${a.graded_posts.toLocaleString()} posts graded`
                : "No graded posts yet"
            }
            tip={
              <Tip title="Reach forecast (actual ÷ projected)">
                <p>
                  Hot Posts ranks posts mainly by how many views they&apos;re projected to reach in
                  48 hours. This checks that projection: for each post that could have appeared
                  in Hot Posts, its actual 48-hour views divided by what was projected the first
                  time TweetTracker saw it.
                </p>
                <p>
                  Reading it: 1.0x is a perfect forecast. Above 1.0x means posts end up bigger than
                  projected; below means smaller. &quot;Within 2x&quot; is the share of posts that
                  landed between half and double the projection.
                </p>
                <p>
                  Which posts count: first seen within 90 minutes of being posted, projected above
                  your Hot Posts minimum, and now 48 hours old.
                </p>
              </Tip>
            }
          />
          <MetricCard
            label="Pick lift"
            value={a.pick_lift === null ? "—" : fmtMult(a.pick_lift)}
            valueClass="text-sky-300"
            sub={
              a.pick_lift === null
                ? `Needs ${a.pick_lift_min_samples}+ replies each way (now ${a.hot_posts_replies} from Hot Posts, ${a.other_replies} other)`
                : `${fmtInt(a.hot_posts_median_views)} vs ${fmtInt(a.other_median_views)} median views`
            }
            tip={
              <Tip title="Pick lift">
                <p>
                  Median views on replies you sent from Hot Posts (clicking Reply or Quote on a
                  card), divided by median views on your other replies, over the last{" "}
                  {a.pick_lift_window_days} days.
                </p>
                <p>
                  Reading it: above 1.0x means Hot Posts picks get your replies more views than the
                  posts you pick yourself. Below 1.0x means it isn&apos;t helping yet.
                </p>
                <p>
                  It stays blank until there are at least {a.pick_lift_min_samples} replies in each
                  group, because a couple of replies can swing it either way.
                </p>
              </Tip>
            }
          />
          <MetricCard
            label="How early"
            value={fmtPct(a.median_share_views_still_ahead)}
            valueClass="text-emerald-300"
            sub="of a post's views still to come when first seen"
            tip={
              <Tip title="How early">
                <p>
                  For the same posts as Reach forecast: the share of each post&apos;s final 48-hour
                  views that came after TweetTracker first saw it (median across posts).
                </p>
                <p>
                  Reading it: higher is better. 90% means that when a post first appears, 90% of
                  the people who will ever see it haven&apos;t yet, so a reply posted then sits
                  near the top of a thread most viewers are still to arrive at.
                </p>
              </Tip>
            }
          />
        </div>
      </div>
    </div>
  );
}
