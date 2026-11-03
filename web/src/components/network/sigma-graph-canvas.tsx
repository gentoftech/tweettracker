"use client";

import React, { useEffect, useMemo, useRef, useState } from "react";
import Graph from "graphology";
import Sigma from "sigma";
import type { Settings } from "sigma/settings";
import type { EdgeDisplayData, NodeDisplayData, PartialButFor } from "sigma/types";
import { Maximize2, Sparkles, ZoomIn, ZoomOut } from "lucide-react";
import type { BridgeRole, GraphNode, NetworkGraphResponse } from "@/lib/types";

export interface SigmaGraphCanvasProps {
  graphData: NetworkGraphResponse;
  mode: "community" | "bridges";
  sizeBy: "heavy_hitter" | "followers";
  roleFilter?: "all" | BridgeRole;
  highlightFilter?: "all" | "rising" | "follow_back" | "top_movers";
  searchQuery?: string;
  selectedAccountId: number | null;
  onSelectAccount: (accountId: number) => void;
}

function hexWithAlpha(hex: string, alphaHex: string): string {
  const clean = (hex || "#38bdf8").trim();
  if (/^#[0-9a-fA-F]{6}$/.test(clean)) {
    return `${clean}${alphaHex}`;
  }
  return clean;
}

function compactNumber(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return String(n);
}

function drawDarkNodeHover(
  context: CanvasRenderingContext2D,
  data: PartialButFor<NodeDisplayData, "x" | "y" | "size" | "label" | "color">,
  settings: Settings
): void {
  const size = settings.labelSize || 12;
  const font = settings.labelFont || "Inter, system-ui, sans-serif";
  const weight = settings.labelWeight || "600";

  context.save();
  context.font = `${weight} ${size}px ${font}`;

  context.beginPath();
  context.arc(data.x, data.y, data.size + 4.5, 0, Math.PI * 2);
  context.fillStyle = hexWithAlpha(data.color, "2e");
  context.fill();
  context.lineWidth = 1.75;
  context.strokeStyle = data.color || "#38bdf8";
  context.stroke();

  if (typeof data.label === "string" && data.label.length > 0) {
    const paddingX = 8;
    const paddingY = 5;
    const textWidth = context.measureText(data.label).width;
    const boxW = textWidth + paddingX * 2;
    const boxH = size + paddingY * 2;
    const boxX = data.x + data.size + 6;
    const boxY = data.y - boxH / 2;
    const r = 6;

    context.beginPath();
    context.moveTo(boxX + r, boxY);
    context.lineTo(boxX + boxW - r, boxY);
    context.quadraticCurveTo(boxX + boxW, boxY, boxX + boxW, boxY + r);
    context.lineTo(boxX + boxW, boxY + boxH - r);
    context.quadraticCurveTo(boxX + boxW, boxY + boxH, boxX + boxW - r, boxY + boxH);
    context.lineTo(boxX + r, boxY + boxH);
    context.quadraticCurveTo(boxX, boxY + boxH, boxX, boxY + boxH - r);
    context.lineTo(boxX, boxY + r);
    context.quadraticCurveTo(boxX, boxY, boxX + r, boxY);
    context.closePath();

    context.fillStyle = "rgba(13, 19, 34, 0.96)";
    context.fill();
    context.lineWidth = 1.25;
    context.strokeStyle = data.color || "#38bdf8";
    context.stroke();

    context.fillStyle = "#f8fafc";
    context.textBaseline = "middle";
    context.fillText(data.label, boxX + paddingX, data.y + 0.5);
  }

  context.restore();
}

export default function SigmaGraphCanvas({
  graphData,
  mode,
  sizeBy,
  roleFilter = "all",
  highlightFilter = "all",
  searchQuery = "",
  selectedAccountId,
  onSelectAccount,
}: SigmaGraphCanvasProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const sigmaRef = useRef<Sigma | null>(null);
  const graphRef = useRef<Graph | null>(null);
  const hoveredKeyRef = useRef<string | null>(null);
  const neighborsRef = useRef<Set<string>>(new Set());

  const [hoveredNode, setHoveredNode] = useState<GraphNode | null>(null);
  const [webglFailed, setWebglFailed] = useState<boolean>(false);

  const onSelectAccountRef = useRef(onSelectAccount);
  useEffect(() => {
    onSelectAccountRef.current = onSelectAccount;
  }, [onSelectAccount]);

  const nodeById = useMemo(() => {
    const map = new Map<number, GraphNode>();
    for (const n of graphData.nodes) {
      map.set(n.id, n);
    }
    return map;
  }, [graphData.nodes]);

  const maxFollowers = useMemo(() => {
    let m = 1000;
    for (const n of graphData.nodes) {
      if (n.followers_count > m) m = n.followers_count;
    }
    return m;
  }, [graphData.nodes]);

  const builtGraph = useMemo(() => {
    const g = new Graph({ multi: false, type: "undirected" });

    const sortedForLabels = [...graphData.nodes].sort((a, b) =>
      sizeBy === "followers"
        ? b.followers_count - a.followers_count
        : b.community_heavy_hitter_score - a.community_heavy_hitter_score
    );
    const forcedLabelIds = new Set(sortedForLabels.slice(0, 10).map((n) => n.id));

    for (const n of graphData.nodes) {
      const key = `acct-${n.id}`;
      const pos = mode === "bridges" ? n.n_pole_xy : n.single_community_xy;

      let radius = 8;
      if (sizeBy === "followers") {
        const ratio =
          Math.sqrt(Math.max(n.followers_count, 50)) / Math.sqrt(Math.max(maxFollowers, 100));
        radius = 4.5 + ratio * 18.0;
      } else {
        const inf =
          mode === "bridges" ? n.heavy_hitter_score : n.community_heavy_hitter_score;
        radius = 4.8 + (Math.max(0, inf) / 100) * 17.0;
      }

      const nodeColor = n.color || "#38bdf8";

      g.addNode(key, {
        x: pos.x,
        y: pos.y,
        size: Number(radius.toFixed(2)),
        label: `@${n.handle}`,
        color: nodeColor,
        forceLabel: forcedLabelIds.has(n.id),
        zIndex: forcedLabelIds.has(n.id) ? 20 : 10,
        accountId: n.id,
        handle: n.handle,
        displayName: n.display_name,
        followersCount: n.followers_count,
        bridgeRole: n.bridge_role,
        isMutual20x: n.is_mutual_20x,
        isAutoTracked: Boolean(n.is_auto_tracked),
        relationshipState: n.relationship_state,
        influenceScore: n.community_heavy_hitter_score,
      });
    }

    let maxW = 1;
    for (const e of graphData.edges) {
      if (e.weight > maxW) maxW = e.weight;
    }

    for (const e of graphData.edges) {
      const sKey = `acct-${e.source}`;
      const tKey = `acct-${e.target}`;
      if (!g.hasNode(sKey) || !g.hasNode(tKey) || sKey === tKey) continue;
      if (g.hasEdge(sKey, tKey)) continue;

      const normW = Math.min(1, Math.log1p(e.weight) / Math.log1p(maxW));
      const thickness = 0.65 + normW * 2.2;

      let edgeColor = "rgba(100, 116, 139, 0.22)";
      if (e.is_mutual) {
        edgeColor = "rgba(16, 185, 129, 0.42)";
      } else if (mode === "bridges" && e.cross_community) {
        edgeColor = "rgba(167, 139, 250, 0.38)";
      } else if (e.edge_type === "mutual_reply" || e.edge_type === "quote") {
        edgeColor = "rgba(56, 189, 248, 0.34)";
      }

      g.addEdgeWithKey(e.id, sKey, tKey, {
        size: Number(thickness.toFixed(2)),
        color: edgeColor,
        zIndex: e.cross_community || e.is_mutual ? 8 : 3,
        isChord: false,
        crossCommunity: e.cross_community,
        isMutual: e.is_mutual,
      });
    }

    return g;
  }, [graphData, mode, sizeBy, maxFollowers]);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    graphRef.current = builtGraph;
    hoveredKeyRef.current = null;
    neighborsRef.current = new Set();
    setHoveredNode(null);

    let renderer: Sigma | null = null;
    try {
      renderer = new Sigma(builtGraph, container, {
        allowInvalidContainer: true,
        renderLabels: true,
        labelFont: "Inter, ui-sans-serif, system-ui, sans-serif",
        labelSize: 11,
        labelWeight: "600",
        labelColor: { color: "#e2e8f0" },
        labelRenderedSizeThreshold: 8.5,
        stagePadding: 44,
        zIndex: true,
        minCameraRatio: 0.18,
        maxCameraRatio: 3.5,
        defaultDrawNodeHover: drawDarkNodeHover,
      });

      renderer.on("enterNode", ({ node }) => {
        hoveredKeyRef.current = node;
        const neigh = new Set<string>(builtGraph.neighbors(node));
        neigh.add(node);
        neighborsRef.current = neigh;

        const attrs = builtGraph.getNodeAttributes(node);
        if (typeof attrs.accountId === "number") {
          setHoveredNode(nodeById.get(attrs.accountId) ?? null);
        } else {
          setHoveredNode(null);
        }
        container.style.cursor = "pointer";
        renderer?.refresh({ skipIndexation: true });
      });

      renderer.on("leaveNode", () => {
        hoveredKeyRef.current = null;
        neighborsRef.current = new Set();
        setHoveredNode(null);
        container.style.cursor = "grab";
        renderer?.refresh({ skipIndexation: true });
      });

      renderer.on("clickNode", ({ node }) => {
        const attrs = builtGraph.getNodeAttributes(node);
        if (typeof attrs.accountId === "number") {
          onSelectAccountRef.current(attrs.accountId);
        }
      });

      sigmaRef.current = renderer;
      setWebglFailed(false);
    } catch {
      setWebglFailed(true);
    }

    return () => {
      if (renderer) {
        renderer.kill();
      }
      sigmaRef.current = null;
    };
  }, [builtGraph, nodeById]);

  useEffect(() => {
    const renderer = sigmaRef.current;
    const g = graphRef.current;
    if (!renderer || !g) return;

    const q = searchQuery.trim().toLowerCase();
    const selectedKey = selectedAccountId != null ? `acct-${selectedAccountId}` : null;

    renderer.setSetting("nodeReducer", (node, data) => {
      const res: Partial<NodeDisplayData> = { ...data };

      const matchesRole =
        roleFilter === "all" || data.bridgeRole === roleFilter;

      let matchesHighlight = true;
      if (highlightFilter === "rising") {
        matchesHighlight =
          Boolean(data.isAutoTracked) && Number(data.followersCount || 0) < 50_000;
      } else if (highlightFilter === "follow_back") {
        matchesHighlight = data.relationshipState === "inbound_unreciprocated";
      } else if (highlightFilter === "top_movers") {
        matchesHighlight = Number(data.influenceScore || 0) >= 60;
      }

      const handleLower = String(data.handle || "").toLowerCase();
      const dispLower = String(data.displayName || "").toLowerCase();
      const matchesSearch =
        !q || handleLower.includes(q) || dispLower.includes(q);

      const hoveredKey = hoveredKeyRef.current;
      if (hoveredKey) {
        if (neighborsRef.current.has(node)) {
          res.forceLabel = true;
          res.zIndex = node === hoveredKey ? 40 : 25;
          if (node === hoveredKey) {
            res.highlighted = true;
          }
        } else {
          res.color = "rgba(51, 65, 85, 0.30)";
          res.label = null;
          res.forceLabel = false;
          res.zIndex = 2;
        }
        return res;
      }

      if (selectedKey && node === selectedKey) {
        res.highlighted = true;
        res.forceLabel = true;
        res.size = (data.size || 8) * 1.25;
        res.zIndex = 38;
        return res;
      }

      if (!matchesRole || !matchesHighlight || !matchesSearch) {
        res.color = "rgba(51, 65, 85, 0.25)";
        res.label = null;
        res.forceLabel = false;
        res.size = Math.max(3.2, (data.size || 6) * 0.72);
        res.zIndex = 2;
      } else if ((q && matchesSearch) || highlightFilter !== "all") {
        res.forceLabel = true;
        res.zIndex = 26;
      }

      return res;
    });

    renderer.setSetting("edgeReducer", (edge, data) => {
      const res: Partial<EdgeDisplayData> = { ...data };
      if (data.isChord) return res;
      const hoveredKey = hoveredKeyRef.current;
      if (hoveredKey) {
        const [src, tgt] = g.extremities(edge);
        if (src === hoveredKey || tgt === hoveredKey) {
          res.color = data.isMutual
            ? "rgba(16, 185, 129, 0.9)"
            : "rgba(56, 189, 248, 0.85)";
          res.size = Math.max(1.8, (data.size || 1) * 1.6);
          res.zIndex = 22;
        } else {
          res.color = "rgba(30, 41, 59, 0.14)";
          res.zIndex = 1;
        }
      }
      return res;
    });

    renderer.refresh({ skipIndexation: true });
  }, [builtGraph, mode, roleFilter, highlightFilter, searchQuery, selectedAccountId]);

  if (graphData.nodes.length === 0) {
    return (
      <div className="flex h-[560px] flex-col items-center justify-center rounded-2xl border border-slate-800 bg-surface-900/70 p-8 text-center">
        <Sparkles className="h-8 w-8 text-slate-500" />
        <p className="mt-3 text-sm font-medium text-slate-300">
          No people tracked yet.
        </p>
        <p className="mt-1 text-xs text-slate-500">
          Click + Add People to import handles or sync an X List.
        </p>
      </div>
    );
  }

  return (
    <div className="relative h-[580px] w-full overflow-hidden rounded-2xl border border-slate-800/90 bg-[#070c18] shadow-inner">
      <div
        className="pointer-events-none absolute inset-0 opacity-35"
        style={{
          backgroundImage:
            "radial-gradient(circle at 50% 50%, rgba(56, 189, 248, 0.10) 0%, rgba(15, 23, 42, 0.03) 48%, transparent 75%)",
        }}
      />

      {!webglFailed ? (
        <div
          ref={containerRef}
          className="h-full w-full cursor-grab active:cursor-grabbing"
          aria-label="Interactive Community Graph"
        />
      ) : (
        <svg
          viewBox="-1.35 -1.35 2.7 2.7"
          className="h-full w-full"
          aria-label="Interactive Community Graph"
        >
          {graphData.edges.map((e) => {
            const s = nodeById.get(e.source);
            const t = nodeById.get(e.target);
            if (!s || !t) return null;
            const pS = mode === "bridges" ? s.n_pole_xy : s.single_community_xy;
            const pT = mode === "bridges" ? t.n_pole_xy : t.single_community_xy;
            return (
              <line
                key={e.id}
                x1={pS.x}
                y1={-pS.y}
                x2={pT.x}
                y2={-pT.y}
                stroke={e.is_mutual ? "rgba(16,185,129,0.45)" : "rgba(100,116,139,0.28)"}
                strokeWidth={0.008}
              />
            );
          })}
          {graphData.nodes.map((n) => {
            const p = mode === "bridges" ? n.n_pole_xy : n.single_community_xy;
            const r = 0.02 + (n.community_heavy_hitter_score / 100) * 0.05;
            return (
              <circle
                key={`svg-node-${n.id}`}
                cx={p.x}
                cy={-p.y}
                r={r}
                fill={n.color}
                stroke={selectedAccountId === n.id ? "#ffffff" : "#0f172a"}
                strokeWidth={selectedAccountId === n.id ? 0.012 : 0.005}
                className="cursor-pointer"
                onClick={() => onSelectAccount(n.id)}
              />
            );
          })}
        </svg>
      )}

      {/* Top-left subtle orientation hint */}
      <div className="pointer-events-none absolute left-3.5 top-3.5">
        <div className="rounded-lg border border-slate-800/90 bg-surface-900/90 px-2.5 py-1.5 text-[11px] text-slate-300 backdrop-blur">
          Connected people cluster naturally · Node size = influence · Color = interaction cluster
        </div>
      </div>

      {/* Camera controls */}
      <div className="absolute right-3.5 top-3.5 flex items-center gap-1 rounded-lg border border-slate-800 bg-surface-900/90 p-1 backdrop-blur">
        <button
          type="button"
          onClick={() => sigmaRef.current?.getCamera().animatedZoom({ duration: 200 })}
          title="Zoom in"
          className="rounded p-1.5 text-slate-300 transition hover:bg-slate-800 hover:text-white"
        >
          <ZoomIn className="h-3.5 w-3.5" />
        </button>
        <button
          type="button"
          onClick={() => sigmaRef.current?.getCamera().animatedUnzoom({ duration: 200 })}
          title="Zoom out"
          className="rounded p-1.5 text-slate-300 transition hover:bg-slate-800 hover:text-white"
        >
          <ZoomOut className="h-3.5 w-3.5" />
        </button>
        <button
          type="button"
          onClick={() => sigmaRef.current?.getCamera().animatedReset({ duration: 240 })}
          title="Fit graph"
          className="rounded p-1.5 text-slate-300 transition hover:bg-slate-800 hover:text-white"
        >
          <Maximize2 className="h-3.5 w-3.5" />
        </button>
      </div>

      {/* Plain-English Hover Card */}
      {hoveredNode && (
        <div className="pointer-events-none absolute bottom-3.5 left-3.5 max-w-xs rounded-xl border border-slate-700/90 bg-surface-900/95 p-3 shadow-2xl backdrop-blur">
          <div className="flex items-center justify-between gap-2">
            <div className="truncate">
              <span className="text-xs font-semibold text-white">
                {hoveredNode.display_name}
              </span>
              <span className="ml-1.5 text-[11px] text-sky-400">
                @{hoveredNode.handle}
              </span>
            </div>
            {hoveredNode.is_mutual_20x ? (
              <span className="shrink-0 rounded bg-emerald-500/20 px-1.5 py-0.5 text-[10px] font-semibold text-emerald-300">
                Mutual
              </span>
            ) : hoveredNode.is_auto_tracked ? (
              <span className="shrink-0 rounded bg-cyan-500/20 px-1.5 py-0.5 text-[10px] font-semibold text-cyan-300">
                Rising
              </span>
            ) : null}
          </div>

          <div className="mt-2 grid grid-cols-3 gap-2 text-[11px]">
            <div>
              <div className="text-slate-500">Influence</div>
              <div className="font-semibold text-white">
                {Math.round(hoveredNode.community_heavy_hitter_score)}/100
              </div>
            </div>
            <div>
              <div className="text-slate-500">Spark Lift</div>
              <div className="font-semibold text-amber-300">
                {hoveredNode.catalysis_lift.toFixed(1)}x
              </div>
            </div>
            <div>
              <div className="text-slate-500">Followers</div>
              <div className="font-semibold text-slate-200">
                {compactNumber(hoveredNode.followers_count)}
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
