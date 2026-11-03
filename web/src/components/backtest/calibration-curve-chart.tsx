"use client";

import React, { useState } from "react";
import type { CalibrationBin } from "@/lib/backtest-types";

interface CalibrationCurveChartProps {
  bins: CalibrationBin[];
  comparisonBins?: CalibrationBin[];
  comparisonLabel?: string;
  brierScore: number;
  ece?: number;
}

export function CalibrationCurveChart({
  bins,
  comparisonBins,
  comparisonLabel = "Candidate",
  brierScore,
  ece,
}: CalibrationCurveChartProps) {
  const [hoveredBin, setHoveredBin] = useState<number | null>(null);

  const width = 560;
  const height = 340;
  const padLeft = 52;
  const padRight = 24;
  const padTop = 20;
  const plotHeight = 205;
  const histTop = padTop + plotHeight + 28;
  const histHeight = 58;
  const plotWidth = width - padLeft - padRight;

  const toX = (p: number) => padLeft + Math.max(0, Math.min(1, p)) * plotWidth;
  const toY = (rate: number) =>
    padTop + (1 - Math.max(0, Math.min(1, rate))) * plotHeight;

  const activeBins = bins.filter(
    (b) =>
      b.count > 0 &&
      b.predicted_prob_mean !== null &&
      b.observed_breakout_rate !== null
  );

  const compActiveBins = (comparisonBins ?? []).filter(
    (b) =>
      b.count > 0 &&
      b.predicted_prob_mean !== null &&
      b.observed_breakout_rate !== null
  );

  const maxCount = Math.max(
    1,
    ...bins.map((b) => b.count),
    ...(comparisonBins ?? []).map((b) => b.count)
  );

  const polylinePoints = activeBins
    .map(
      (b) =>
        `${toX(b.predicted_prob_mean ?? b.bin_midpoint).toFixed(1)},${toY(
          b.observed_breakout_rate ?? 0
        ).toFixed(1)}`
    )
    .join(" ");

  const compPolylinePoints = compActiveBins
    .map(
      (b) =>
        `${toX(b.predicted_prob_mean ?? b.bin_midpoint).toFixed(1)},${toY(
          b.observed_breakout_rate ?? 0
        ).toFixed(1)}`
    )
    .join(" ");

  const selected = hoveredBin !== null ? bins[hoveredBin] : null;
  const selectedComp =
    hoveredBin !== null && comparisonBins ? comparisonBins[hoveredBin] : null;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h3 className="text-sm font-semibold text-white">
            10-Bin Brier Reliability &amp; Calibration Curve
          </h3>
          <p className="text-xs text-slate-400">
            Predicted breakout probability vs. empirical 48h breakout rate against ideal{" "}
            <code className="text-slate-300">y = x</code> diagonal
          </p>
        </div>
        <div className="flex items-center gap-2 text-xs font-mono">
          <span className="px-2.5 py-1 rounded-md bg-violet-500/15 border border-violet-500/30 text-violet-300">
            Brier: {brierScore.toFixed(4)}
          </span>
          {ece !== undefined && (
            <span className="px-2.5 py-1 rounded-md bg-sky-500/15 border border-sky-500/30 text-sky-300">
              ECE: {ece.toFixed(4)}
            </span>
          )}
        </div>
      </div>

      <div className="relative rounded-xl bg-surface-950/80 border border-slate-800/90 p-3">
        <svg
          viewBox={`0 0 ${width} ${height}`}
          className="w-full h-auto select-none overflow-visible"
          role="img"
          aria-label="10-bin Brier calibration reliability chart"
        >
          {/* Horizontal & Vertical Grid Lines */}
          {[0, 0.25, 0.5, 0.75, 1.0].map((tick) => {
            const y = toY(tick);
            const x = toX(tick);
            return (
              <g key={tick}>
                <line
                  x1={padLeft}
                  y1={y}
                  x2={padLeft + plotWidth}
                  y2={y}
                  stroke="#1e293b"
                  strokeWidth={1}
                />
                <line
                  x1={x}
                  y1={padTop}
                  x2={x}
                  y2={padTop + plotHeight}
                  stroke="#1e293b"
                  strokeWidth={1}
                />
                <text
                  x={padLeft - 8}
                  y={y + 4}
                  textAnchor="end"
                  className="fill-slate-400 text-[10px] font-mono"
                >
                  {(tick * 100).toFixed(0)}%
                </text>
                <text
                  x={x}
                  y={padTop + plotHeight + 14}
                  textAnchor="middle"
                  className="fill-slate-400 text-[10px] font-mono"
                >
                  {(tick * 100).toFixed(0)}%
                </text>
              </g>
            );
          })}

          {/* Ideal y = x Perfect Calibration Diagonal */}
          <line
            x1={toX(0)}
            y1={toY(0)}
            x2={toX(1)}
            y2={toY(1)}
            stroke="#64748b"
            strokeWidth={1.5}
            strokeDasharray="5 4"
          />
          <text
            x={toX(0.82)}
            y={toY(0.86)}
            className="fill-slate-400 text-[9px] font-mono"
          >
            Ideal y = x
          </text>

          {/* Residual Stems from y=x to Observed Rate */}
          {activeBins.map((b) => {
            const px = b.predicted_prob_mean ?? b.bin_midpoint;
            const obs = b.observed_breakout_rate ?? 0;
            return (
              <line
                key={`stem-${b.bin_index}`}
                x1={toX(px)}
                y1={toY(px)}
                x2={toX(px)}
                y2={toY(obs)}
                stroke={obs >= px ? "#34d399" : "#f43f5e"}
                strokeOpacity={0.45}
                strokeWidth={2}
              />
            );
          })}

          {/* Live / Primary Reliability Curve */}
          {activeBins.length > 1 && (
            <polyline
              fill="none"
              stroke="#38bdf8"
              strokeWidth={2.5}
              points={polylinePoints}
            />
          )}

          {/* Optional Candidate Overlay Curve */}
          {compActiveBins.length > 1 && (
            <polyline
              fill="none"
              stroke="#34d399"
              strokeWidth={2.2}
              strokeDasharray="4 2"
              points={compPolylinePoints}
            />
          )}

          {/* Primary Data Points */}
          {activeBins.map((b) => {
            const px = b.predicted_prob_mean ?? b.bin_midpoint;
            const obs = b.observed_breakout_rate ?? 0;
            const isHover = hoveredBin === b.bin_index;
            const radius = Math.min(8.5, 4 + Math.sqrt(b.count) * 0.9);
            return (
              <circle
                key={`pt-${b.bin_index}`}
                cx={toX(px)}
                cy={toY(obs)}
                r={isHover ? radius + 2 : radius}
                className="cursor-pointer transition-all"
                fill="#0ea5e9"
                fillOpacity={0.9}
                stroke="#e0f2fe"
                strokeWidth={isHover ? 2 : 1.2}
                onMouseEnter={() => setHoveredBin(b.bin_index)}
                onMouseLeave={() => setHoveredBin(null)}
              />
            );
          })}

          {/* Optional Candidate Data Points */}
          {compActiveBins.map((b) => {
            const px = b.predicted_prob_mean ?? b.bin_midpoint;
            const obs = b.observed_breakout_rate ?? 0;
            return (
              <circle
                key={`cpt-${b.bin_index}`}
                cx={toX(px)}
                cy={toY(obs)}
                r={4}
                fill="#10b981"
                stroke="#d1fae5"
                strokeWidth={1}
                pointerEvents="none"
              />
            );
          })}

          {/* Bottom Bin Sample Density Histogram */}
          <line
            x1={padLeft}
            y1={histTop + histHeight}
            x2={padLeft + plotWidth}
            y2={histTop + histHeight}
            stroke="#334155"
            strokeWidth={1}
          />
          <text
            x={padLeft - 8}
            y={histTop + 10}
            textAnchor="end"
            className="fill-slate-500 text-[9px] font-mono"
          >
            Bin N
          </text>

          {bins.map((b) => {
            const binW = plotWidth / 10;
            const x0 = padLeft + b.bin_index * binW + 3;
            const barW = comparisonBins ? (binW - 8) / 2 : binW - 6;
            const h1 = (b.count / maxCount) * histHeight;
            const compBin = comparisonBins?.[b.bin_index];
            const h2 = compBin ? (compBin.count / maxCount) * histHeight : 0;
            const isHover = hoveredBin === b.bin_index;

            return (
              <g
                key={`hist-${b.bin_index}`}
                className="cursor-pointer"
                onMouseEnter={() => setHoveredBin(b.bin_index)}
                onMouseLeave={() => setHoveredBin(null)}
              >
                <rect
                  x={padLeft + b.bin_index * binW}
                  y={padTop}
                  width={binW}
                  height={height - padTop}
                  fill={isHover ? "#38bdf8" : "transparent"}
                  fillOpacity={isHover ? 0.05 : 0}
                />
                <rect
                  x={x0}
                  y={histTop + histHeight - h1}
                  width={Math.max(2, barW)}
                  height={Math.max(b.count > 0 ? 2 : 0, h1)}
                  rx={2}
                  fill={isHover ? "#38bdf8" : "#0284c7"}
                  fillOpacity={0.75}
                />
                {compBin && (
                  <rect
                    x={x0 + barW + 2}
                    y={histTop + histHeight - h2}
                    width={Math.max(2, barW)}
                    height={Math.max(compBin.count > 0 ? 2 : 0, h2)}
                    rx={2}
                    fill="#10b981"
                    fillOpacity={0.75}
                  />
                )}
                {b.count > 0 && (
                  <text
                    x={padLeft + (b.bin_index + 0.5) * binW}
                    y={histTop + histHeight + 12}
                    textAnchor="middle"
                    className="fill-slate-400 text-[9px] font-mono"
                  >
                    {b.count}
                  </text>
                )}
              </g>
            );
          })}
        </svg>

        {/* Legend & Hover Readout Bar */}
        <div className="mt-2 pt-2 border-t border-slate-800/80 flex flex-wrap items-center justify-between gap-2 text-xs">
          <div className="flex items-center gap-4 text-slate-400">
            <span className="inline-flex items-center gap-1.5">
              <span className="h-2.5 w-2.5 rounded-full bg-sky-400" />
              <span>{comparisonBins ? "Live Active" : "Observed 48h Rate"}</span>
            </span>
            {comparisonBins && (
              <span className="inline-flex items-center gap-1.5">
                <span className="h-2.5 w-2.5 rounded-full bg-emerald-400" />
                <span>{comparisonLabel}</span>
              </span>
            )}
            <span className="inline-flex items-center gap-1.5">
              <span className="w-4 border-t border-dashed border-slate-400" />
              <span>Perfect Calibration</span>
            </span>
          </div>

          {selected ? (
            <div className="font-mono text-[11px] text-slate-200 bg-surface-850 border border-slate-700 px-2.5 py-1 rounded-md">
              Bin [{(selected.bin_lower * 100).toFixed(0)}–
              {(selected.bin_upper * 100).toFixed(0)}%] · N={selected.count}
              {selected.predicted_prob_mean !== null &&
              selected.observed_breakout_rate !== null
                ? ` · Pred ${(selected.predicted_prob_mean * 100).toFixed(1)}% vs Obs ${(
                    selected.observed_breakout_rate * 100
                  ).toFixed(1)}%`
                : " · Empty bin"}
              {selectedComp && selectedComp.count > 0 && (
                <span className="text-emerald-300">
                  {" "}
                  | Cand N={selectedComp.count} (Obs{" "}
                  {((selectedComp.observed_breakout_rate ?? 0) * 100).toFixed(1)}%)
                </span>
              )}
            </div>
          ) : (
            <span className="text-[11px] text-slate-500">
              Hover any probability bin to inspect predicted vs. 48h realized breakout rate
            </span>
          )}
        </div>
      </div>
    </div>
  );
}
