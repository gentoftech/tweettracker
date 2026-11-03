"use client";

import React, { useEffect, useState } from "react";
import {
  ArrowUpRight,
  ExternalLink,
  Gauge,
  Play,
  Rocket,
  RotateCcw,
  Sliders,
  Zap,
} from "lucide-react";
import type {
  BacktestReplayResponse,
  ChangelogVersion,
  FlippedPostItem,
  SandboxCandidateParams,
} from "@/lib/backtest-types";
import { CalibrationCurveChart } from "./calibration-curve-chart";

interface SandboxTabProps {
  activeVersion: ChangelogVersion | null;
  defaultWatchThreshold: number;
  defaultSurgeThreshold: number;
  replayResult: BacktestReplayResponse | null;
  replaying: boolean;
  promoting: boolean;
  onRunReplay: (params: SandboxCandidateParams) => void;
  onPromoteCandidate: (params: SandboxCandidateParams) => void;
}

interface SliderControlDef {
  key: keyof SandboxCandidateParams;
  label: string;
  rustName: string;
  min: number;
  max: number;
  step: number;
  group: "thresholds" | "weights" | "surrogate";
}

const SLIDER_CONTROLS: SliderControlDef[] = [
  {
    key: "watch_threshold",
    label: "Watch Alert Threshold",
    rustName: "WATCH_THRESHOLD",
    min: 35,
    max: 92,
    step: 0.5,
    group: "thresholds",
  },
  {
    key: "surge_threshold",
    label: "Surge Alert Threshold",
    rustName: "SURGE_THRESHOLD",
    min: 50,
    max: 98,
    step: 0.5,
    group: "thresholds",
  },
  {
    key: "dpp_theta",
    label: "DPP Diversity Theta",
    rustName: "DppTheta",
    min: 0.1,
    max: 0.95,
    step: 0.01,
    group: "thresholds",
  },
  {
    key: "breakout_multiple",
    label: "48h Breakout Multiple",
    rustName: "BREAKOUT_MULTIPLE",
    min: 1.5,
    max: 8.0,
    step: 0.1,
    group: "thresholds",
  },
  {
    key: "bidirectional_follow_reply_weight_boost",
    label: "Mutual Follow Reply Boost",
    rustName: "BidirectionalFollowReplyWeightBoost",
    min: 0,
    max: 40,
    step: 0.5,
    group: "weights",
  },
  {
    key: "share_via_copy_link",
    label: "Share via Copy-Link Weight",
    rustName: "share_via_copy_link",
    min: 1,
    max: 50,
    step: 0.5,
    group: "weights",
  },
  {
    key: "quote",
    label: "Quote-Post Weight",
    rustName: "quote",
    min: 0.5,
    max: 25,
    step: 0.25,
    group: "weights",
  },
  {
    key: "reply",
    label: "Base Reply Weight",
    rustName: "reply",
    min: 0.5,
    max: 25,
    step: 0.25,
    group: "weights",
  },
  {
    key: "not_interested",
    label: "Not Interested Penalty",
    rustName: "not_interested",
    min: -250,
    max: -5,
    step: 1,
    group: "weights",
  },
  {
    key: "prior_views_alpha0",
    label: "Empirical-Bayes Prior Views α₀",
    rustName: "prior_views_alpha0",
    min: 25,
    max: 800,
    step: 5,
    group: "surrogate",
  },
  {
    key: "copy_link_from_bookmark_mult",
    label: "Bookmark → Copy-Link Mult",
    rustName: "copy_link_from_bookmark_mult",
    min: 0.05,
    max: 1.0,
    step: 0.01,
    group: "surrogate",
  },
  {
    key: "url_dwell_discount",
    label: "External URL Dwell Discount",
    rustName: "url_dwell_discount",
    min: 0.2,
    max: 1.0,
    step: 0.01,
    group: "surrogate",
  },
];

function extractDefaultsFromVersion(
  ver: ChangelogVersion | null,
  defaultWatch: number,
  defaultSurge: number
): Required<
  Pick<
    SandboxCandidateParams,
    | "watch_threshold"
    | "surge_threshold"
    | "breakout_multiple"
    | "bidirectional_follow_reply_weight_boost"
    | "share_via_copy_link"
    | "quote"
    | "reply"
    | "not_interested"
    | "dpp_theta"
    | "prior_views_alpha0"
    | "copy_link_from_bookmark_mult"
    | "url_dwell_discount"
  >
> {
  const w = ver?.weights ?? {};
  const s = ver?.surrogate_params ?? {};
  const th = ver?.thresholds ?? {};
  const pipe =
    th.pipeline && typeof th.pipeline === "object"
      ? (th.pipeline as Record<string, unknown>)
      : {};

  return {
    watch_threshold:
      typeof th.watch_threshold === "number" ? th.watch_threshold : defaultWatch,
    surge_threshold:
      typeof th.surge_threshold === "number" ? th.surge_threshold : defaultSurge,
    breakout_multiple:
      typeof th.breakout_multiple === "number" ? th.breakout_multiple : 3.0,
    bidirectional_follow_reply_weight_boost:
      typeof w.bidirectional_follow_reply_weight_boost === "number"
        ? w.bidirectional_follow_reply_weight_boost
        : 15.0,
    share_via_copy_link:
      typeof w.share_via_copy_link === "number" ? w.share_via_copy_link : 20.0,
    quote: typeof w.quote === "number" ? w.quote : 5.0,
    reply: typeof w.reply === "number" ? w.reply : 5.0,
    not_interested:
      typeof w.not_interested === "number" ? w.not_interested : -47.52,
    dpp_theta: typeof pipe.dpp_theta === "number" ? pipe.dpp_theta : 0.65,
    prior_views_alpha0:
      typeof s.prior_views_alpha0 === "number" ? s.prior_views_alpha0 : 200.0,
    copy_link_from_bookmark_mult:
      typeof s.copy_link_from_bookmark_mult === "number"
        ? s.copy_link_from_bookmark_mult
        : 0.38,
    url_dwell_discount:
      typeof s.url_dwell_discount === "number" ? s.url_dwell_discount : 0.72,
  };
}

function transitionBadge(transition: FlippedPostItem["transition"]) {
  switch (transition) {
    case "fn_to_tp":
      return {
        label: "FN → TP · Newly Caught Hit",
        cls: "bg-emerald-500/15 border-emerald-500/40 text-emerald-300",
      };
    case "fp_to_tn":
      return {
        label: "FP → TN · Filtered Fizzle",
        cls: "bg-sky-500/15 border-sky-500/40 text-sky-300",
      };
    case "tn_to_fp":
      return {
        label: "TN → FP · New False Alarm",
        cls: "bg-amber-500/15 border-amber-500/40 text-amber-300",
      };
    case "tp_to_fn":
      return {
        label: "TP → FN · Newly Missed",
        cls: "bg-rose-500/15 border-rose-500/40 text-rose-300",
      };
  }
}

export function SandboxTab({
  activeVersion,
  defaultWatchThreshold,
  defaultSurgeThreshold,
  replayResult,
  replaying,
  promoting,
  onRunReplay,
  onPromoteCandidate,
}: SandboxTabProps) {
  const [formState, setFormState] = useState(() =>
    extractDefaultsFromVersion(
      activeVersion,
      defaultWatchThreshold,
      defaultSurgeThreshold
    )
  );
  const [promoteLabel, setPromoteLabel] = useState("");
  const [flipFilter, setFlipFilter] = useState<
    "all" | "fn_to_tp" | "fp_to_tn" | "tn_to_fp" | "tp_to_fn"
  >("all");

  useEffect(() => {
    setFormState(
      extractDefaultsFromVersion(
        activeVersion,
        defaultWatchThreshold,
        defaultSurgeThreshold
      )
    );
  }, [activeVersion, defaultWatchThreshold, defaultSurgeThreshold]);

  function handleResetToLive() {
    const defaults = extractDefaultsFromVersion(
      activeVersion,
      defaultWatchThreshold,
      defaultSurgeThreshold
    );
    setFormState(defaults);
    onRunReplay({ ...defaults, benchmark_10k: false });
  }

  function applyPreset(kind: "slow_burn_hunter" | "high_precision_shield") {
    const base = extractDefaultsFromVersion(
      activeVersion,
      defaultWatchThreshold,
      defaultSurgeThreshold
    );
    if (kind === "slow_burn_hunter") {
      const next = {
        ...base,
        watch_threshold: Math.max(48, base.watch_threshold - 10),
        share_via_copy_link: 28.0,
        copy_link_from_bookmark_mult: 0.52,
        quote: 7.5,
      };
      setFormState(next);
      onRunReplay({ ...next, benchmark_10k: false });
    } else {
      const next = {
        ...base,
        watch_threshold: Math.min(88, base.watch_threshold + 14),
        url_dwell_discount: 0.55,
        not_interested: -85.0,
        dpp_theta: 0.55,
      };
      setFormState(next);
      onRunReplay({ ...next, benchmark_10k: false });
    }
  }

  const allDiffs = replayResult?.post_diffs ?? [];
  const filteredDiffs =
    flipFilter === "all"
      ? allDiffs
      : allDiffs.filter((d) => d.transition === flipFilter);

  return (
    <div className="space-y-6">
      {/* Parameter Controls Card */}
      <div className="rounded-2xl border border-slate-800 bg-surface-900/90 p-5 space-y-5">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h3 className="text-sm font-semibold text-white flex items-center gap-2">
              <Sliders className="h-4 w-4 text-sky-400" />
              <span>
                Candidate Stage 1 + Stage 2 Parameter Overrides (vs.{" "}
                <code className="text-slate-200">
                  {activeVersion?.label ?? "9106e9f"}
                </code>
                )
              </span>
            </h3>
            <p className="text-xs text-slate-400">
              Adjust thresholds, 29-head <code className="text-slate-300">xai-value-model</code>{" "}
              weights, and Empirical-Bayes propensities to simulate historical impact in &lt;2s
            </p>
          </div>

          <div className="flex items-center gap-2 flex-wrap">
            <button
              type="button"
              onClick={() => applyPreset("slow_burn_hunter")}
              className="px-2.5 py-1.5 rounded-lg bg-emerald-500/10 hover:bg-emerald-500/20 border border-emerald-500/30 text-emerald-300 text-xs font-medium transition-colors"
            >
              Preset: Catch Slow Burns
            </button>
            <button
              type="button"
              onClick={() => applyPreset("high_precision_shield")}
              className="px-2.5 py-1.5 rounded-lg bg-sky-500/10 hover:bg-sky-500/20 border border-sky-500/30 text-sky-300 text-xs font-medium transition-colors"
            >
              Preset: Cut Early Fizzles
            </button>
            <button
              type="button"
              onClick={handleResetToLive}
              className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg bg-surface-850 hover:bg-surface-800 border border-slate-700 text-slate-300 text-xs font-medium transition-colors"
            >
              <RotateCcw className="h-3.5 w-3.5" />
              <span>Reset</span>
            </button>
          </div>
        </div>

        {/* 12 Synchronized Slider + Numeric Controls */}
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3.5">
          {SLIDER_CONTROLS.map((ctrl) => {
            const val = Number(
              formState[ctrl.key as keyof typeof formState] ?? ctrl.min
            );
            return (
              <div
                key={ctrl.key}
                className="rounded-xl bg-surface-850/90 border border-slate-800 p-3 space-y-2"
              >
                <div className="flex items-center justify-between gap-2">
                  <div className="min-w-0">
                    <div className="text-xs font-medium text-slate-200 truncate">
                      {ctrl.label}
                    </div>
                    <div className="text-[10px] font-mono text-slate-500 truncate">
                      {ctrl.rustName}
                    </div>
                  </div>
                  <input
                    type="number"
                    min={ctrl.min}
                    max={ctrl.max}
                    step={ctrl.step}
                    value={val}
                    onChange={(e) => {
                      const parsed = parseFloat(e.target.value);
                      if (!Number.isNaN(parsed)) {
                        setFormState((prev) => ({ ...prev, [ctrl.key]: parsed }));
                      }
                    }}
                    className="w-20 rounded bg-surface-900 border border-slate-700 px-2 py-1 text-right text-xs font-mono text-white focus:outline-none focus:border-sky-500"
                  />
                </div>
                <input
                  type="range"
                  min={ctrl.min}
                  max={ctrl.max}
                  step={ctrl.step}
                  value={val}
                  onChange={(e) =>
                    setFormState((prev) => ({
                      ...prev,
                      [ctrl.key]: parseFloat(e.target.value),
                    }))
                  }
                  className="w-full accent-sky-400 cursor-pointer h-1.5 bg-slate-800 rounded-lg"
                />
              </div>
            );
          })}
        </div>

        {/* Action & Benchmark Bar */}
        <div className="pt-2 border-t border-slate-800 flex flex-wrap items-center justify-between gap-3">
          <div className="flex flex-wrap items-center gap-2.5">
            <button
              type="button"
              onClick={() => onRunReplay({ ...formState, benchmark_10k: false })}
              disabled={replaying}
              className="inline-flex items-center gap-2 px-4 py-2.5 rounded-xl bg-sky-500 hover:bg-sky-400 text-slate-950 text-xs font-semibold transition-colors disabled:opacity-50"
            >
              <Play className="h-3.5 w-3.5 fill-current" />
              <span>{replaying ? "Replaying..." : "Run Backtest Replay"}</span>
            </button>

            <button
              type="button"
              onClick={() =>
                onRunReplay({
                  ...formState,
                  benchmark_10k: true,
                  benchmark_size: 10000,
                })
              }
              disabled={replaying}
              className="inline-flex items-center gap-2 px-4 py-2.5 rounded-xl bg-violet-500/20 hover:bg-violet-500/30 border border-violet-500/40 text-violet-200 text-xs font-semibold transition-colors disabled:opacity-50"
            >
              <Zap className="h-3.5 w-3.5 text-violet-400" />
              <span>Run 10,000-Post Speed Benchmark</span>
            </button>

            {replayResult && (
              <span className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full bg-emerald-500/15 border border-emerald-500/35 text-emerald-300 text-xs font-mono">
                <Gauge className="h-3.5 w-3.5" />
                <span>
                  {replayResult.elapsed_ms.toFixed(1)} ms for{" "}
                  {replayResult.trajectories_evaluated.toLocaleString()} trajectories
                  {" · "}
                  {Math.round(replayResult.throughput_posts_per_sec).toLocaleString()} posts/s
                </span>
              </span>
            )}
          </div>

          {/* Promote Candidate to Live */}
          <div className="flex items-center gap-2">
            <input
              type="text"
              value={promoteLabel}
              onChange={(e) => setPromoteLabel(e.target.value)}
              placeholder="Optional version label..."
              className="rounded-lg bg-surface-850 border border-slate-700 px-3 py-2 text-xs text-white placeholder:text-slate-500 focus:outline-none focus:border-emerald-500 w-48"
            />
            <button
              type="button"
              onClick={() =>
                onPromoteCandidate({
                  ...formState,
                  label: promoteLabel.trim() || undefined,
                })
              }
              disabled={promoting}
              className="inline-flex items-center gap-1.5 px-4 py-2 rounded-xl bg-emerald-500 hover:bg-emerald-400 text-slate-950 text-xs font-semibold transition-colors disabled:opacity-50"
            >
              <Rocket className="h-3.5 w-3.5" />
              <span>{promoting ? "Promoting..." : "Promote Candidate to Live"}</span>
            </button>
          </div>
        </div>
      </div>

      {/* Replay Side-by-Side Comparison & Flipped Posts */}
      {replayResult && (
        <>
          <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3.5">
            {[
              {
                title: "Precision",
                live: `${(replayResult.live_metrics.precision * 100).toFixed(1)}%`,
                cand: `${(replayResult.candidate_metrics.precision * 100).toFixed(1)}%`,
                delta: replayResult.deltas.delta_precision * 100,
                suffix: "pp",
                lowerIsBetter: false,
              },
              {
                title: "Recall",
                live: `${(replayResult.live_metrics.recall * 100).toFixed(1)}%`,
                cand: `${(replayResult.candidate_metrics.recall * 100).toFixed(1)}%`,
                delta: replayResult.deltas.delta_recall * 100,
                suffix: "pp",
                lowerIsBetter: false,
              },
              {
                title: "F0.5 Score",
                live: replayResult.live_metrics.f05_score.toFixed(3),
                cand: replayResult.candidate_metrics.f05_score.toFixed(3),
                delta: replayResult.deltas.delta_f05,
                suffix: "",
                lowerIsBetter: false,
              },
              {
                title: "Brier Error",
                live: replayResult.live_metrics.brier_score.toFixed(4),
                cand: replayResult.candidate_metrics.brier_score.toFixed(4),
                delta: replayResult.deltas.delta_brier,
                suffix: "",
                lowerIsBetter: true,
              },
              {
                title: "Alerts / Day",
                live: replayResult.live_metrics.alerts_per_day.toFixed(1),
                cand: replayResult.candidate_metrics.alerts_per_day.toFixed(1),
                delta: replayResult.deltas.delta_alerts_per_day,
                suffix: "/d",
                lowerIsBetter: false,
              },
              {
                title: "Net Flips",
                live: `${replayResult.flips_summary.fn_to_tp} Caught`,
                cand: `${replayResult.flips_summary.fp_to_tn} Filtered`,
                delta:
                  replayResult.flips_summary.fn_to_tp +
                  replayResult.flips_summary.fp_to_tn -
                  replayResult.flips_summary.tn_to_fp -
                  replayResult.flips_summary.tp_to_fn,
                suffix: " net",
                lowerIsBetter: false,
              },
            ].map((card) => {
              const improved = card.lowerIsBetter
                ? card.delta < -1e-5
                : card.delta > 1e-5;
              const degraded = card.lowerIsBetter
                ? card.delta > 1e-5
                : card.delta < -1e-5;
              return (
                <div
                  key={card.title}
                  className="rounded-xl bg-surface-900 border border-slate-800 p-4 space-y-1.5"
                >
                  <div className="text-[11px] font-medium text-slate-400">
                    {card.title}
                  </div>
                  <div className="flex items-baseline justify-between gap-2 font-mono">
                    <span className="text-xs text-slate-400">{card.live}</span>
                    <ArrowUpRight className="h-3 w-3 text-slate-500 shrink-0" />
                    <span className="text-base font-semibold text-white">
                      {card.cand}
                    </span>
                  </div>
                  <div
                    className={`text-[11px] font-mono ${
                      improved
                        ? "text-emerald-400"
                        : degraded
                        ? "text-rose-400"
                        : "text-slate-500"
                    }`}
                  >
                    {card.delta > 0 ? "+" : ""}
                    {card.title === "Brier Error"
                      ? card.delta.toFixed(4)
                      : card.title === "F0.5 Score"
                      ? card.delta.toFixed(3)
                      : card.delta.toFixed(1)}
                    {card.suffix}
                  </div>
                </div>
              );
            })}
          </div>

          {/* Dual Calibration Overlay + Post-Level Classification Diffs */}
          <div className="grid grid-cols-1 lg:grid-cols-12 gap-5">
            <div className="lg:col-span-5 rounded-2xl border border-slate-800 bg-surface-900/80 p-5">
              <CalibrationCurveChart
                bins={replayResult.live_metrics.calibration_bins}
                comparisonBins={replayResult.candidate_metrics.calibration_bins}
                comparisonLabel="Candidate Override"
                brierScore={replayResult.candidate_metrics.brier_score}
                ece={replayResult.candidate_metrics.ece}
              />
            </div>

            <div className="lg:col-span-7 rounded-2xl border border-slate-800 bg-surface-900/80 p-5 space-y-4">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div>
                  <h3 className="text-sm font-semibold text-white">
                    Post-Level Classification Flips ({allDiffs.length} Stored Posts Changed)
                  </h3>
                  <p className="text-xs text-slate-400">
                    Exact historical threads that cross alert thresholds between Live and Candidate
                  </p>
                </div>

                <div className="flex items-center gap-1.5 flex-wrap text-[11px] font-mono">
                  {(
                    [
                      ["all", `All (${allDiffs.length})`],
                      ["fn_to_tp", `FN→TP (${replayResult.flips_summary.fn_to_tp})`],
                      ["fp_to_tn", `FP→TN (${replayResult.flips_summary.fp_to_tn})`],
                      ["tn_to_fp", `TN→FP (${replayResult.flips_summary.tn_to_fp})`],
                      ["tp_to_fn", `TP→FN (${replayResult.flips_summary.tp_to_fn})`],
                    ] as const
                  ).map(([key, label]) => (
                    <button
                      key={key}
                      type="button"
                      onClick={() => setFlipFilter(key)}
                      className={`px-2 py-1 rounded-md border transition-colors ${
                        flipFilter === key
                          ? "bg-sky-500/20 border-sky-500/50 text-sky-200"
                          : "bg-surface-850 border-slate-800 text-slate-400 hover:text-slate-200"
                      }`}
                    >
                      {label}
                    </button>
                  ))}
                </div>
              </div>

              {filteredDiffs.length === 0 ? (
                <div className="rounded-xl bg-surface-850/60 border border-slate-800 p-8 text-center text-xs text-slate-400">
                  No stored workspace posts flipped alert classification for this filter. Adjust{" "}
                  <code className="text-slate-200">watch_threshold</code> or{" "}
                  <code className="text-slate-200">share_via_copy_link</code> to inspect boundary
                  crossings.
                </div>
              ) : (
                <div className="space-y-2.5 max-h-[430px] overflow-y-auto pr-1">
                  {filteredDiffs.map((row) => {
                    const badge = transitionBadge(row.transition);
                    return (
                      <div
                        key={row.post_id}
                        className="rounded-xl bg-surface-850/90 border border-slate-800 p-3.5 space-y-2"
                      >
                        <div className="flex flex-wrap items-center justify-between gap-2">
                          <div className="flex items-center gap-2 flex-wrap">
                            <span
                              className={`text-[11px] font-semibold px-2 py-0.5 rounded-full border ${badge.cls}`}
                            >
                              {badge.label}
                            </span>
                            <span className="text-xs font-semibold text-white">
                              @{row.author_handle}
                            </span>
                            {row.communities.map((c) => (
                              <span
                                key={c.id}
                                className="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded bg-slate-800 text-slate-300"
                              >
                                <span
                                  className="h-1.5 w-1.5 rounded-full"
                                  style={{ backgroundColor: c.color }}
                                />
                                {c.name}
                              </span>
                            ))}
                          </div>
                          <a
                            href={row.x_status_url}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="inline-flex items-center gap-1 text-[11px] text-slate-400 hover:text-sky-300"
                          >
                            <span>X Status</span>
                            <ExternalLink className="h-3 w-3" />
                          </a>
                        </div>

                        <p className="text-xs text-slate-300 line-clamp-2">{row.text}</p>

                        <div className="flex flex-wrap items-center justify-between gap-2 text-[11px] font-mono text-slate-400 pt-1">
                          <span>
                            Score:{" "}
                            <strong className="text-slate-200">
                              {row.live_score.toFixed(1)}
                            </strong>{" "}
                            →{" "}
                            <strong className="text-white">
                              {row.candidate_score.toFixed(1)}
                            </strong>{" "}
                            ({row.delta_score > 0 ? "+" : ""}
                            {row.delta_score.toFixed(1)})
                          </span>
                          <span>
                            48h Outcome:{" "}
                            <strong
                              className={
                                row.is_true_breakout_48h
                                  ? "text-emerald-300"
                                  : "text-amber-300"
                              }
                            >
                              {row.actual_48h_views.toLocaleString()} views (
                              {row.actual_48h_multiple.toFixed(2)}x P50)
                            </strong>
                          </span>
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          </div>
        </>
      )}
    </div>
  );
}
