"use client";

import React, { useState } from "react";
import {
  CheckCircle2,
  Clock,
  GitCommit,
  History,
  Lock,
  RotateCcw,
  ShieldCheck,
  Sliders,
  Sparkles,
} from "lucide-react";
import type { CalibrateResult, ChangelogResponse } from "@/lib/backtest-types";

interface ChangelogTabProps {
  changelog: ChangelogResponse | null;
  maturedCount: number;
  calibrating: boolean;
  activatingId: number | null;
  lastCalibration: CalibrateResult | null;
  onRunAutoTuner: (opts: { force_stage_only: boolean }) => void;
  onActivateVersion: (versionId: number) => void;
}

function sourceBadge(source: string) {
  switch (source) {
    case "auto_tuner":
      return {
        label: "L2 Auto-Tuner",
        cls: "bg-violet-500/15 border-violet-500/35 text-violet-300",
      };
    case "manual_sandbox":
      return {
        label: "Sandbox Promotion",
        cls: "bg-amber-500/15 border-amber-500/35 text-amber-300",
      };
    case "upstream_xalgo":
    default:
      return {
        label: "Upstream x-algorithm",
        cls: "bg-sky-500/15 border-sky-500/35 text-sky-300",
      };
  }
}

export function ChangelogTab({
  changelog,
  maturedCount,
  calibrating,
  activatingId,
  lastCalibration,
  onRunAutoTuner,
  onActivateVersion,
}: ChangelogTabProps) {
  const [stageOnly, setStageOnly] = useState(false);

  const versions = changelog?.versions ?? [];

  return (
    <div className="space-y-6">
      {/* Holdout-Gated Regularized Auto-Tuner Control Card */}
      <div className="rounded-2xl border border-slate-800 bg-surface-900/90 p-5 space-y-4">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="space-y-1.5 max-w-2xl">
            <div className="flex items-center gap-2 flex-wrap">
              <span className="inline-flex items-center gap-1.5 text-sm font-semibold text-white">
                <Sparkles className="h-4 w-4 text-violet-400" />
                Holdout-Gated L2-Regularized Auto-Tuner
              </span>
              <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] font-mono bg-emerald-500/15 border border-emerald-500/35 text-emerald-300">
                <ShieldCheck className="h-3 w-3" />
                ±20% Max Single-Run Drift Cap
              </span>
              <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] font-mono bg-sky-500/15 border border-sky-500/35 text-sky-300">
                <Lock className="h-3 w-3" />
                Chronological 80/20 Split
              </span>
            </div>
            <p className="text-xs text-slate-400 leading-relaxed">
              Fits L2-shrunk surrogate propensities, Stage 1/2 fusion weights, and watch/surge
              thresholds on the oldest 80% matured trajectories (anchored to{" "}
              <code className="text-slate-200">xai-value-model @ 9106e9f</code> priors), promoting
              only if newest 20% out-of-time holdout Brier or F0.5 improves. Requires ≥30 matured
              posts (current workspace: <strong className="text-white font-mono">{maturedCount}</strong>).
            </p>
          </div>

          <div className="flex flex-col sm:flex-row items-stretch sm:items-center gap-3">
            <label className="inline-flex items-center gap-2 text-xs text-slate-300 cursor-pointer select-none px-3 py-2 rounded-lg bg-surface-850 border border-slate-800">
              <input
                type="checkbox"
                checked={stageOnly}
                onChange={(e) => setStageOnly(e.target.checked)}
                className="rounded border-slate-600 bg-surface-900 text-violet-500 focus:ring-0"
              />
              <span>Stage only (review before live)</span>
            </label>

            <button
              type="button"
              onClick={() => onRunAutoTuner({ force_stage_only: stageOnly })}
              disabled={calibrating}
              className="inline-flex items-center justify-center gap-2 px-4 py-2.5 rounded-xl bg-violet-500 hover:bg-violet-400 text-slate-950 text-xs font-semibold transition-colors disabled:opacity-50 shadow-sm"
            >
              <Sliders className={`h-4 w-4 ${calibrating ? "animate-spin" : ""}`} />
              <span>
                {calibrating
                  ? "Optimizing L2 Calibration..."
                  : "Run Holdout-Gated Auto-Tuner"}
              </span>
            </button>
          </div>
        </div>

        {/* Latest Run Result Banner */}
        {lastCalibration && (
          <div
            className={`rounded-xl border p-4 text-xs space-y-2 ${
              lastCalibration.status === "insufficient_data" ||
              lastCalibration.status === "insufficient_breakouts"
                ? "bg-amber-500/10 border-amber-500/35 text-amber-200"
                : lastCalibration.promoted
                ? "bg-emerald-500/10 border-emerald-500/35 text-emerald-200"
                : "bg-sky-500/10 border-sky-500/35 text-sky-200"
            }`}
          >
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="font-semibold flex items-center gap-2">
                <CheckCircle2 className="h-4 w-4 shrink-0" />
                <span>
                  {lastCalibration.status === "insufficient_data"
                    ? `Calibration Guardrail Refused (${lastCalibration.matured_posts_count} / ${lastCalibration.min_matured_posts} matured posts)`
                    : lastCalibration.status === "insufficient_breakouts"
                    ? "Calibration Guardrail Refused (not enough true breakouts yet)"
                    : lastCalibration.promoted
                    ? "Holdout Gate PASSED — Candidate Auto-Promoted to Live"
                    : "Candidate Version Staged in Changelog"}
                </span>
              </div>
              {lastCalibration.split && (
                <span className="font-mono text-[11px] px-2 py-0.5 rounded bg-surface-900/80 border border-slate-700 text-slate-200">
                  Train N={lastCalibration.split.train_count} → Holdout N=
                  {lastCalibration.split.holdout_count} · Zero Leakage:{" "}
                  {lastCalibration.split.zero_future_leakage ? "VERIFIED" : "NO"} · Max Drift:{" "}
                  {(lastCalibration.max_observed_drift_pct ?? 0).toFixed(1)}%
                </span>
              )}
            </div>

            <p className="text-slate-200/90 font-mono text-[11px] leading-relaxed">
              {lastCalibration.reason}
            </p>
          </div>
        )}
      </div>

      {/* Parameter Version Timeline & 1-Click Rollback */}
      <div className="space-y-3">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-semibold text-white flex items-center gap-2">
            <History className="h-4 w-4 text-sky-400" />
            <span>Parameter Version Changelog ({versions.length})</span>
          </h3>
          <span className="text-xs text-slate-400">
            Instant atomic rollback to any historical or upstream{" "}
            <code className="text-slate-300">9106e9f</code> version
          </span>
        </div>

        <div className="space-y-3.5">
          {versions.map((ver) => {
            const badge = sourceBadge(ver.source);
            const hm = ver.holdout_metrics || {};
            const isActivating = activatingId === ver.id;

            return (
              <div
                key={ver.id}
                className={`rounded-2xl border p-5 transition-all space-y-3.5 ${
                  ver.is_effective_active
                    ? "bg-surface-900 border-emerald-500/50 shadow-lg shadow-emerald-950/20"
                    : "bg-surface-900/70 border-slate-800 hover:border-slate-700"
                }`}
              >
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="space-y-1">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="font-mono text-xs font-bold px-2 py-0.5 rounded bg-slate-800 text-white">
                        v{ver.version_number}
                      </span>
                      <span className="text-sm font-semibold text-white">
                        {ver.label}
                      </span>
                      <span
                        className={`text-[11px] font-medium px-2 py-0.5 rounded-full border ${badge.cls}`}
                      >
                        {badge.label}
                      </span>
                      {ver.is_effective_active ? (
                        <span className="inline-flex items-center gap-1 text-[11px] font-semibold px-2.5 py-0.5 rounded-full bg-emerald-500/20 border border-emerald-500/40 text-emerald-300">
                          <span className="h-1.5 w-1.5 rounded-full bg-emerald-400 animate-pulse" />
                          LIVE ACTIVE
                        </span>
                      ) : (
                        <span className="text-[11px] px-2 py-0.5 rounded-full bg-slate-800/90 text-slate-400">
                          {ver.workspace_id === null ? "Global Baseline" : "Historical / Staged"}
                        </span>
                      )}
                    </div>

                    <div className="flex items-center gap-3 text-[11px] text-slate-400 font-mono">
                      <span className="inline-flex items-center gap-1">
                        <GitCommit className="h-3 w-3 text-sky-400" />
                        HEAD {ver.upstream_commit_sha}
                      </span>
                      <span>·</span>
                      <span className="inline-flex items-center gap-1">
                        <Clock className="h-3 w-3 text-slate-500" />
                        {new Date(ver.created_at).toLocaleString()}
                      </span>
                    </div>
                  </div>

                  {/* Holdout Metrics & 1-Click Activate / Rollback */}
                  <div className="flex items-center gap-3 flex-wrap">
                    <div className="flex items-center gap-2 text-[11px] font-mono">
                      {typeof hm.f05 === "number" && (
                        <span className="px-2 py-1 rounded bg-surface-850 border border-slate-800 text-cyan-300">
                          F0.5: {hm.f05.toFixed(3)}
                        </span>
                      )}
                      {typeof hm.brier === "number" && (
                        <span className="px-2 py-1 rounded bg-surface-850 border border-slate-800 text-violet-300">
                          Brier: {hm.brier.toFixed(4)}
                        </span>
                      )}
                      {typeof hm.precision === "number" && (
                        <span className="px-2 py-1 rounded bg-surface-850 border border-slate-800 text-emerald-300">
                          P: {(hm.precision * 100).toFixed(0)}%
                        </span>
                      )}
                    </div>

                    {!ver.is_effective_active && (
                      <button
                        type="button"
                        onClick={() => onActivateVersion(ver.id)}
                        disabled={activatingId !== null}
                        className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-sky-500/15 hover:bg-sky-500/25 border border-sky-500/40 text-sky-200 text-xs font-semibold transition-colors disabled:opacity-50"
                      >
                        <RotateCcw
                          className={`h-3.5 w-3.5 ${isActivating ? "animate-spin" : ""}`}
                        />
                        <span>
                          {isActivating ? "Activating..." : "1-Click Rollback / Activate"}
                        </span>
                      </button>
                    )}
                  </div>
                </div>

                {/* Changelog Note */}
                {ver.changelog_note && (
                  <p className="text-xs text-slate-300 bg-surface-850/70 border border-slate-800/80 rounded-lg px-3 py-2 leading-relaxed">
                    {ver.changelog_note}
                  </p>
                )}

                {/* BEFORE -> AFTER Parameter Diff Pills */}
                {ver.param_diffs && ver.param_diffs.length > 0 && (
                  <div className="flex flex-wrap gap-1.5 pt-0.5">
                    {ver.param_diffs.map((d) => {
                      const isUp = (d.pct_change ?? 0) > 0;
                      return (
                        <span
                          key={`${d.section}-${d.param}`}
                          className="inline-flex items-center gap-1.5 text-[11px] font-mono px-2.5 py-1 rounded-md bg-surface-850 border border-slate-700/80 text-slate-200"
                        >
                          <span className="text-slate-400">{d.const_name}:</span>
                          <span className="text-slate-400">{String(d.before)}</span>
                          <span className="text-slate-500">→</span>
                          <span className="text-white font-semibold">{String(d.after)}</span>
                          {d.pct_change !== null && (
                            <span
                              className={
                                isUp ? "text-emerald-400" : "text-amber-400"
                              }
                            >
                              ({d.pct_change > 0 ? "+" : ""}
                              {d.pct_change.toFixed(1)}%)
                            </span>
                          )}
                        </span>
                      );
                    })}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
