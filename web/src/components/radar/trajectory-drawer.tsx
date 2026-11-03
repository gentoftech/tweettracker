"use client";

import React, { useEffect, useMemo, useState } from "react";
import { Activity, TrendingUp, X } from "lucide-react";
import { radarApi } from "@/lib/radar-api";
import type {
  RadarPostDetailResponse,
  TrajectoryComparisonPoint,
} from "@/lib/radar-types";

interface TrajectoryDrawerProps {
  workspaceId: number;
  postId: number;
  onClose?: () => void;
}

function formatAgeLabel(minutes: number): string {
  if (minutes < 60) return `${Math.round(minutes)}m`;
  if (minutes < 1440) {
    const hrs = minutes / 60;
    return Number.isInteger(hrs) ? `${hrs}h` : `${hrs.toFixed(1)}h`;
  }
  const days = minutes / 1440;
  return Number.isInteger(days) ? `${days * 24}h` : `${(minutes / 60).toFixed(0)}h`;
}

export function TrajectoryDrawer({
  workspaceId,
  postId,
  onClose,
}: TrajectoryDrawerProps) {
  const [detail, setDetail] = useState<RadarPostDetailResponse | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [hoveredIdx, setHoveredIdx] = useState<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    radarApi
      .getPostDetail(workspaceId, postId)
      .then((res) => {
        if (!cancelled) {
          setDetail(res);
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setError(
            err instanceof Error ? err.message : "Failed to load growth curve."
          );
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [workspaceId, postId]);

  const chartGeometry = useMemo(() => {
    if (!detail || detail.trajectory_comparison.length === 0) return null;
    const pts = detail.trajectory_comparison;
    const width = 680;
    const height = 210;
    const padLeft = 52;
    const padRight = 20;
    const padTop = 16;
    const padBottom = 28;
    const plotW = width - padLeft - padRight;
    const plotH = height - padTop - padBottom;

    const minLog = Math.log(Math.max(10, pts[0].age_minutes));
    const maxLog = Math.log(Math.max(2880, pts[pts.length - 1].age_minutes));

    let maxY = 100;
    for (const p of pts) {
      maxY = Math.max(maxY, p.p90, p.p75, p.actual_views ?? 0);
    }
    maxY = maxY * 1.12;

    const xOf = (ageMinutes: number) => {
      const clamped = Math.max(10, ageMinutes);
      const ratio = (Math.log(clamped) - minLog) / Math.max(0.0001, maxLog - minLog);
      return padLeft + ratio * plotW;
    };

    const yOf = (views: number) => {
      const ratio = Math.min(1, Math.max(0, views / maxY));
      return padTop + plotH - ratio * plotH;
    };

    const toPolyline = (getter: (p: TrajectoryComparisonPoint) => number) =>
      pts.map((p) => `${xOf(p.age_minutes).toFixed(1)},${yOf(getter(p)).toFixed(1)}`).join(" ");

    const bandPolygon = [
      ...pts.map((p) => `${xOf(p.age_minutes).toFixed(1)},${yOf(p.p75).toFixed(1)}`),
      ...[...pts]
        .reverse()
        .map((p) => `${xOf(p.age_minutes).toFixed(1)},${yOf(p.p25).toFixed(1)}`),
    ].join(" ");

    const actualPts = pts
      .map((p, index) =>
        p.actual_views !== null
          ? {
              index,
              point: p,
              x: xOf(p.age_minutes),
              y: yOf(p.actual_views),
            }
          : null
      )
      .filter((item): item is NonNullable<typeof item> => item !== null);

    const actualPolyline = actualPts
      .map((item) => `${item.x.toFixed(1)},${item.y.toFixed(1)}`)
      .join(" ");

    return {
      width,
      height,
      padLeft,
      padRight,
      padTop,
      padBottom,
      maxY,
      pts,
      xOf,
      yOf,
      bandPolygon,
      p50Line: toPolyline((p) => p.p50),
      p90Line: toPolyline((p) => p.p90),
      actualPts,
      actualPolyline,
    };
  }, [detail]);

  if (loading) {
    return (
      <div className="mt-4 rounded-xl border border-slate-800 bg-surface-950/90 p-4 text-xs text-slate-400 flex items-center gap-2">
        <Activity className="h-4 w-4 animate-spin text-sky-400" />
        <span>Loading view growth curve...</span>
      </div>
    );
  }

  if (error || !detail) {
    return (
      <div className="mt-4 rounded-xl border border-rose-500/30 bg-rose-950/20 p-4 text-xs text-rose-300 flex items-center justify-between">
        <span>{error || "Unable to load growth curve."}</span>
        {onClose && (
          <button
            type="button"
            onClick={onClose}
            className="text-rose-200 hover:text-white"
          >
            <X className="h-4 w-4" />
          </button>
        )}
      </div>
    );
  }

  const { velocity_detail, value_model_breakdown } = detail;

  const activePoint =
    hoveredIdx !== null && detail.trajectory_comparison[hoveredIdx]
      ? detail.trajectory_comparison[hoveredIdx]
      : detail.trajectory_comparison.find(
          (p) => Math.abs(p.age_minutes - detail.post.age_minutes) < 1
        ) ?? detail.trajectory_comparison[0];

  const topHeads = [
    ...value_model_breakdown.top_positive_heads.slice(0, 5),
    ...value_model_breakdown.negative_heads.slice(0, 2),
  ];

  return (
    <div
      className="mt-4 rounded-xl border border-slate-800 bg-surface-950/95 p-4 space-y-4"
      onClick={(e) => e.stopPropagation()}
    >
      {/* Header */}
      <div className="flex items-center justify-between gap-2 pb-2.5 border-b border-slate-800/80">
        <span className="inline-flex items-center gap-1.5 text-xs font-semibold text-slate-200">
          <TrendingUp className="h-4 w-4 text-sky-400" />
          <span>View Growth vs. @{detail.post.author.handle}&apos;s Typical Post</span>
        </span>
        {onClose && (
          <button
            type="button"
            onClick={onClose}
            className="inline-flex items-center gap-1 px-2 py-1 rounded-lg bg-surface-900 hover:bg-surface-800 border border-slate-800 text-xs text-slate-300"
          >
            <X className="h-3.5 w-3.5" />
            <span>Close</span>
          </button>
        )}
      </div>

      {/* Curve + Plain-English Pace Summary */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <div className="lg:col-span-2 rounded-xl bg-surface-900/90 border border-slate-800 p-3.5">
          <div className="flex flex-wrap items-center justify-between gap-2 mb-2 text-[11px]">
            <span className="text-slate-400">15m to 48h trajectory</span>
            <div className="flex items-center gap-3">
              <span className="inline-flex items-center gap-1 text-emerald-300">
                <span className="h-2 w-2 rounded-full bg-emerald-400" />
                This Post
              </span>
              <span className="inline-flex items-center gap-1 text-amber-300">
                <span className="h-0.5 w-3 bg-amber-400" />
                Top 10% Line
              </span>
              <span className="inline-flex items-center gap-1 text-sky-300">
                <span className="h-2 w-2 rounded-xs bg-sky-500/30 border border-sky-400/50" />
                Usual Range
              </span>
            </div>
          </div>

          {chartGeometry && (
            <div className="w-full overflow-x-auto">
              <svg
                viewBox={`0 0 ${chartGeometry.width} ${chartGeometry.height}`}
                className="w-full h-48 select-none"
              >
                {[0.25, 0.5, 0.75, 1.0].map((frac) => {
                  const val = Math.round(chartGeometry.maxY * frac);
                  const y = chartGeometry.yOf(val);
                  return (
                    <g key={frac}>
                      <line
                        x1={chartGeometry.padLeft}
                        x2={chartGeometry.width - chartGeometry.padRight}
                        y1={y}
                        y2={y}
                        stroke="#1e293b"
                        strokeDasharray="3 3"
                      />
                      <text
                        x={chartGeometry.padLeft - 6}
                        y={y + 3}
                        textAnchor="end"
                        className="fill-slate-500 text-[9px] font-mono"
                      >
                        {val >= 1000 ? `${(val / 1000).toFixed(1)}k` : val}
                      </text>
                    </g>
                  );
                })}

                <polygon
                  points={chartGeometry.bandPolygon}
                  fill="rgba(56, 189, 248, 0.13)"
                  stroke="rgba(56, 189, 248, 0.25)"
                  strokeWidth="1"
                />
                <polyline
                  fill="none"
                  stroke="#38bdf8"
                  strokeWidth="1.5"
                  strokeDasharray="4 4"
                  points={chartGeometry.p50Line}
                />
                <polyline
                  fill="none"
                  stroke="#fbbf24"
                  strokeWidth="1.5"
                  strokeDasharray="5 3"
                  points={chartGeometry.p90Line}
                />

                {chartGeometry.actualPts.length > 1 && (
                  <polyline
                    fill="none"
                    stroke="#34d399"
                    strokeWidth="2.75"
                    points={chartGeometry.actualPolyline}
                  />
                )}

                {chartGeometry.pts.map((p, idx) => {
                  const x = chartGeometry.xOf(p.age_minutes);
                  const isStandardTick = [15, 30, 45, 60, 120, 360, 1440, 2880].includes(
                    Math.round(p.age_minutes)
                  );
                  return (
                    <g
                      key={`${p.age_minutes}-${idx}`}
                      onMouseEnter={() => setHoveredIdx(idx)}
                      onMouseLeave={() => setHoveredIdx(null)}
                      className="cursor-pointer"
                    >
                      {isStandardTick && (
                        <text
                          x={x}
                          y={chartGeometry.height - 8}
                          textAnchor="middle"
                          className="fill-slate-400 text-[9px] font-mono"
                        >
                          {formatAgeLabel(p.age_minutes)}
                        </text>
                      )}
                      <rect
                        x={x - 10}
                        y={chartGeometry.padTop}
                        width={20}
                        height={
                          chartGeometry.height -
                          chartGeometry.padTop -
                          chartGeometry.padBottom
                        }
                        fill="transparent"
                      />
                    </g>
                  );
                })}

                {chartGeometry.actualPts.map((item) => (
                  <circle
                    key={item.index}
                    cx={item.x}
                    cy={item.y}
                    r={hoveredIdx === item.index ? 5.5 : 4}
                    fill="#10b981"
                    stroke="#070b14"
                    strokeWidth="1.5"
                    onMouseEnter={() => setHoveredIdx(item.index)}
                  />
                ))}
              </svg>
            </div>
          )}

          {activePoint && (
            <div className="mt-2 pt-2 border-t border-slate-800/80 flex flex-wrap items-center justify-between gap-2 text-[11px]">
              <span className="text-slate-300">
                At <strong>{formatAgeLabel(activePoint.age_minutes)}</strong>
              </span>
              <span className="text-sky-300">
                Usual: {Math.round(activePoint.p50).toLocaleString()} views
              </span>
              <span className="text-amber-300">
                Top 10%: {Math.round(activePoint.p90).toLocaleString()} views
              </span>
              <span className="text-emerald-300 font-medium">
                Actual:{" "}
                {activePoint.actual_views !== null
                  ? `${Math.round(activePoint.actual_views).toLocaleString()} views`
                  : "still growing"}
              </span>
            </div>
          )}
        </div>

        {/* Plain-English Summary Side Box */}
        <div className="rounded-xl bg-surface-900/90 border border-slate-800 p-3.5 flex flex-col justify-between gap-3">
          <div className="space-y-2.5">
            <div className="text-xs font-semibold text-white">Momentum Breakdown</div>
            <div className="grid grid-cols-2 gap-2">
              <div className="rounded-lg bg-surface-950 border border-slate-800/90 p-2.5">
                <div className="text-[11px] text-slate-400">Current Pace</div>
                <div className="text-base font-bold text-emerald-300 mt-0.5">
                  {velocity_detail.velocity_multiple.toFixed(1)}x
                </div>
                <div className="text-[10px] text-slate-500">vs author norm</div>
              </div>
              <div className="rounded-lg bg-surface-950 border border-slate-800/90 p-2.5">
                <div className="text-[11px] text-slate-400">Breakout Odds</div>
                <div className="text-base font-bold text-sky-300 mt-0.5">
                  {Math.round(detail.post.breakout_probability * 100)}%
                </div>
                <div className="text-[10px] text-slate-500">48h top-decile</div>
              </div>
            </div>
            <p className="text-xs text-slate-300 leading-relaxed">
              {detail.post.tactical_rationale}
            </p>
          </div>

          <details className="text-[11px] text-slate-400 border-t border-slate-800/80 pt-2">
            <summary className="cursor-pointer hover:text-slate-200 select-none font-medium">
              Technical scoring details
            </summary>
            <div className="mt-2 space-y-1 font-mono text-[10px] text-slate-400">
              {topHeads.map((h) => (
                <div key={h.head} className="flex items-center justify-between">
                  <span className="truncate">{h.head}</span>
                  <span className={h.weighted_contribution >= 0 ? "text-sky-300" : "text-rose-300"}>
                    {h.weighted_contribution >= 0 ? "+" : ""}
                    {h.weighted_contribution.toFixed(2)}
                  </span>
                </div>
              ))}
            </div>
          </details>
        </div>
      </div>
    </div>
  );
}
