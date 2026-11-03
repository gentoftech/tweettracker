"use client";

import React from "react";
import {
  AlertTriangle,
  CheckCircle2,
  ExternalLink,
  Flame,
  RefreshCw,
  ShieldAlert,
} from "lucide-react";
import type { PostmortemItem, ScorecardResponse } from "@/lib/backtest-types";
import { CalibrationCurveChart } from "./calibration-curve-chart";

interface ScorecardTabProps {
  scorecard: ScorecardResponse | null;
  regrading: boolean;
  onRegrade: () => void;
}

function PostmortemCard({
  item,
  variant,
}: {
  item: PostmortemItem;
  variant: "fp" | "fn";
}) {
  const isFp = variant === "fp";
  return (
    <div className="rounded-xl bg-surface-850/90 border border-slate-800 p-4 space-y-2.5 hover:border-slate-700 transition-colors">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-sm font-semibold text-white truncate">
              {item.author_display_name || item.author_handle}
            </span>
            <span className="text-xs font-mono text-sky-400">
              @{item.author_handle}
            </span>
          </div>
        </div>
        <a
          href={item.x_status_url}
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex items-center gap-1 text-[11px] font-medium text-slate-400 hover:text-sky-300 shrink-0"
          title="Open status on X"
        >
          <span>X Post</span>
          <ExternalLink className="h-3 w-3" />
        </a>
      </div>

      <p className="text-xs text-slate-300 line-clamp-2 leading-relaxed">
        {item.text}
      </p>

      {/* Diagnostic Tag & Causal Attribution */}
      <div
        className={`rounded-lg px-3 py-2 border text-xs ${
          isFp
            ? "bg-amber-500/10 border-amber-500/30 text-amber-200"
            : "bg-rose-500/10 border-rose-500/30 text-rose-200"
        }`}
      >
        <div className="font-semibold flex items-center gap-1.5">
          {isFp ? (
            <AlertTriangle className="h-3.5 w-3.5 text-amber-400 shrink-0" />
          ) : (
            <Flame className="h-3.5 w-3.5 text-rose-400 shrink-0" />
          )}
          <span>{item.diagnostic_label}</span>
        </div>
        <p className="text-[11px] text-slate-300/90 mt-0.5 leading-snug">
          {item.diagnostic_reason}
        </p>
      </div>

      {/* Telemetry Footer */}
      <div className="grid grid-cols-4 gap-2 pt-1 text-[11px] font-mono text-slate-400">
        <div className="rounded bg-surface-900 px-2 py-1 border border-slate-800/80">
          <div className="text-[10px] text-slate-500">Early Score</div>
          <div className="text-slate-200 font-semibold">
            {item.breakout_score.toFixed(1)}{" "}
            <span className="text-slate-400 font-normal">
              ({(item.breakout_probability * 100).toFixed(0)}%)
            </span>
          </div>
        </div>
        <div className="rounded bg-surface-900 px-2 py-1 border border-slate-800/80">
          <div className="text-[10px] text-slate-500">
            @{item.alert_age_minutes}m Views
          </div>
          <div className="text-slate-200 font-semibold">
            {item.views_at_alert.toLocaleString()}
          </div>
        </div>
        <div className="rounded bg-surface-900 px-2 py-1 border border-slate-800/80">
          <div className="text-[10px] text-slate-500">48h Views</div>
          <div className="text-slate-200 font-semibold">
            {item.actual_48h_views.toLocaleString()}
          </div>
        </div>
        <div className="rounded bg-surface-900 px-2 py-1 border border-slate-800/80">
          <div className="text-[10px] text-slate-500">48h vs P50</div>
          <div
            className={`font-semibold ${
              item.actual_48h_multiple >= 3.0 ? "text-emerald-300" : "text-amber-300"
            }`}
          >
            {item.actual_48h_multiple.toFixed(2)}x
          </div>
        </div>
      </div>
    </div>
  );
}

export function ScorecardTab({
  scorecard,
  regrading,
  onRegrade,
}: ScorecardTabProps) {
  if (!scorecard) {
    return (
      <div className="rounded-2xl border border-slate-800 bg-surface-900/70 p-8 text-center text-sm text-slate-400">
        Loading 48-hour lifecycle scorecard...
      </div>
    );
  }

  const ov = scorecard.overall;
  const def = scorecard.outcome_definition;

  return (
    <div className="space-y-6">
      {/* Outcome Rule Banner + Re-Grade Action */}
      <div className="rounded-xl bg-surface-900/90 border border-slate-800 px-4 py-3 flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2.5 text-xs text-slate-300">
          <CheckCircle2 className="h-4 w-4 text-emerald-400 shrink-0" />
          <span>
            <strong className="text-white">48h True Breakout Criterion:</strong>{" "}
            <code className="text-emerald-300">
              actual_48h_views ≥ {def.breakout_multiple.toFixed(1)}x
            </code>{" "}
            author P50 48h baseline <strong>AND</strong>{" "}
            <code className="text-sky-300">
              ≥ {def.community_floor_views.toLocaleString()} views
            </code>{" "}
            floor (matching X&apos;s <code className="text-slate-200">SimclustersMaxCandidateAgeHours = 48</code>).
          </span>
        </div>
        <button
          type="button"
          onClick={onRegrade}
          disabled={regrading}
          className="inline-flex items-center gap-2 px-3.5 py-1.5 rounded-lg bg-sky-500/15 hover:bg-sky-500/25 border border-sky-500/40 text-sky-200 text-xs font-semibold transition-colors disabled:opacity-50"
        >
          <RefreshCw className={`h-3.5 w-3.5 ${regrading ? "animate-spin" : ""}`} />
          <span>{regrading ? "Grading..." : "Re-Grade 48h Outcomes"}</span>
        </button>
      </div>

      {/* 6 Institutional KPI Cards */}
      <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3.5">
        <div className="rounded-xl bg-surface-900 border border-slate-800 p-4">
          <div className="text-[11px] font-medium text-slate-400">Precision</div>
          <div className="text-2xl font-semibold text-emerald-300 font-mono mt-1">
            {(ov.precision * 100).toFixed(1)}%
          </div>
          <div className="text-[11px] text-slate-500 font-mono mt-1">
            {ov.tp} TP / {ov.tp + ov.fp} alerts
          </div>
        </div>

        <div className="rounded-xl bg-surface-900 border border-slate-800 p-4">
          <div className="text-[11px] font-medium text-slate-400">Recall</div>
          <div className="text-2xl font-semibold text-sky-300 font-mono mt-1">
            {(ov.recall * 100).toFixed(1)}%
          </div>
          <div className="text-[11px] text-slate-500 font-mono mt-1">
            {ov.tp} / {ov.tp + ov.fn} breakouts
          </div>
        </div>

        <div className="rounded-xl bg-surface-900 border border-slate-800 p-4">
          <div className="text-[11px] font-medium text-slate-400">
            F0.5 High-Signal
          </div>
          <div className="text-2xl font-semibold text-cyan-300 font-mono mt-1">
            {ov.f05_score.toFixed(3)}
          </div>
          <div className="text-[11px] text-slate-500 mt-1">
            Precision-weighted β=0.5
          </div>
        </div>

        <div className="rounded-xl bg-surface-900 border border-slate-800 p-4">
          <div className="text-[11px] font-medium text-slate-400">Brier Score</div>
          <div className="text-2xl font-semibold text-violet-300 font-mono mt-1">
            {ov.brier_score.toFixed(4)}
          </div>
          <div className="text-[11px] text-slate-500 font-mono mt-1">
            ROC-AUC {ov.roc_auc.toFixed(3)}
          </div>
        </div>

        <div className="rounded-xl bg-surface-900 border border-slate-800 p-4">
          <div className="text-[11px] font-medium text-slate-400">
            Median Lead-Time Upside
          </div>
          <div className="text-2xl font-semibold text-amber-300 font-mono mt-1">
            +{(ov.median_lead_time_growth_share * 100).toFixed(0)}%
          </div>
          <div className="text-[11px] text-slate-500 font-mono mt-1">
            {ov.median_lead_time_multiple.toFixed(1)}x reach post-alert
          </div>
        </div>

        <div className="rounded-xl bg-surface-900 border border-slate-800 p-4">
          <div className="text-[11px] font-medium text-slate-400">
            Matured Sample N
          </div>
          <div className="text-2xl font-semibold text-white font-mono mt-1">
            {ov.matured_posts_count}
          </div>
          <div className="text-[11px] text-slate-500 font-mono mt-1">
            {ov.tp}TP · {ov.fp}FP · {ov.fn}FN · {ov.tn}TN
          </div>
        </div>
      </div>

      {/* Calibration Curve */}
      <div className="rounded-2xl border border-slate-800 bg-surface-900/80 p-5">
        <CalibrationCurveChart
          bins={scorecard.calibration_curve}
          brierScore={ov.brier_score}
          ece={ov.ece}
        />
      </div>

      {/* Postmortem Drill-Downs: False Alarms (FP) vs Missed Breakouts (FN) */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">
        {/* Left: False Alarms (Early Fizzles) */}
        <div className="rounded-2xl border border-slate-800 bg-surface-900/80 p-5 space-y-4">
          <div className="flex items-center justify-between">
            <div>
              <h3 className="text-sm font-semibold text-white flex items-center gap-2">
                <ShieldAlert className="h-4 w-4 text-amber-400" />
                <span>
                  False Alarms · Early Fizzles ({scorecard.false_alarms_fp.length} FP)
                </span>
              </h3>
              <p className="text-xs text-slate-400">
                Triggered 15–60m radar alerts that stalled below {def.breakout_multiple}x P50 at 48h
              </p>
            </div>
          </div>

          {scorecard.false_alarms_fp.length === 0 ? (
            <div className="rounded-xl bg-surface-850/60 border border-slate-800 p-6 text-center text-xs text-slate-400">
              Zero false alarms recorded under current thresholds.
            </div>
          ) : (
            <div className="space-y-3 max-h-[540px] overflow-y-auto pr-1">
              {scorecard.false_alarms_fp.map((item) => (
                <PostmortemCard key={item.post_id} item={item} variant="fp" />
              ))}
            </div>
          )}
        </div>

        {/* Right: Missed Breakouts (Slow Burns) */}
        <div className="rounded-2xl border border-slate-800 bg-surface-900/80 p-5 space-y-4">
          <div className="flex items-center justify-between">
            <div>
              <h3 className="text-sm font-semibold text-white flex items-center gap-2">
                <Flame className="h-4 w-4 text-rose-400" />
                <span>
                  Missed Breakouts · Slow Burns ({scorecard.missed_breakouts_fn.length} FN)
                </span>
              </h3>
              <p className="text-xs text-slate-400">
                Sub-threshold at 30m before compounding past {def.breakout_multiple}x P50 by hour 48
              </p>
            </div>
          </div>

          {scorecard.missed_breakouts_fn.length === 0 ? (
            <div className="rounded-xl bg-surface-850/60 border border-slate-800 p-6 text-center text-xs text-slate-400">
              Zero missed 48h breakouts — 100% recall on matured trajectories.
            </div>
          ) : (
            <div className="space-y-3 max-h-[540px] overflow-y-auto pr-1">
              {scorecard.missed_breakouts_fn.map((item) => (
                <PostmortemCard key={item.post_id} item={item} variant="fn" />
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
