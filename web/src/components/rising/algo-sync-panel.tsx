"use client";

import React, { useCallback, useEffect, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  ExternalLink,
  FlaskConical,
  GitBranch,
  GitCommit,
  Play,
  RefreshCw,
  ShieldCheck,
  Zap,
} from "lucide-react";
import { ApiError } from "@/lib/api";
import { risingApi } from "@/lib/rising-api";
import type { AlgoSyncStatusResponse, UpstreamAlgoEventItem } from "@/lib/rising-types";
import { useWorkspace } from "@/components/workspace-context";

function statusBadgeMeta(status: string) {
  switch (status) {
    case "drift_detected":
      return {
        label: "Drift Detected",
        tone: "border-amber-500/40 bg-amber-500/15 text-amber-300",
      };
    case "activated":
      return {
        label: "Weights Activated",
        tone: "border-sky-500/40 bg-sky-500/15 text-sky-300",
      };
    case "ignored_non_ranking":
      return {
        label: "Ignored (Non-Ranking)",
        tone: "border-slate-700 bg-slate-800/80 text-slate-300",
      };
    case "in_sync":
    default:
      return {
        label: "In Sync",
        tone: "border-emerald-500/40 bg-emerald-500/15 text-emerald-300",
      };
  }
}

export function AlgoSyncPanel() {
  const { activeWorkspace, refreshAll, setShowAuthDialog } = useWorkspace();

  const [statusData, setStatusData] = useState<AlgoSyncStatusResponse | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [checkingLive, setCheckingLive] = useState<boolean>(false);
  const [simulating, setSimulating] = useState<boolean>(false);
  const [activatingId, setActivatingId] = useState<number | null>(null);
  const [banner, setBanner] = useState<{
    kind: "success" | "info" | "error";
    text: string;
  } | null>(null);
  const [showWatchedPaths, setShowWatchedPaths] = useState<boolean>(false);

  const loadStatus = useCallback(async () => {
    setLoading(true);
    try {
      const res = await risingApi.getAlgoSyncStatus();
      setStatusData(res);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setShowAuthDialog(true);
      } else {
        setBanner({
          kind: "error",
          text: err instanceof Error ? err.message : "Failed to load Algo Sync status.",
        });
      }
    } finally {
      setLoading(false);
    }
  }, [setShowAuthDialog]);

  useEffect(() => {
    void loadStatus();
  }, [loadStatus]);

  async function handleRunCheck(simulateDrift: boolean) {
    if (simulateDrift) {
      setSimulating(true);
    } else {
      setCheckingLive(true);
    }
    setBanner(null);

    try {
      const res = await risingApi.runAlgoSyncCheck({
        simulateDrift,
        autoCreateIssue: !simulateDrift,
        workspaceId: activeWorkspace?.id ?? null,
      });
      await loadStatus();

      const evt = res.event;
      if (evt.status === "drift_detected") {
        const diffCount = evt.param_diffs_json.length;
        const issueSuffix = evt.github_issue_number
          ? ` · GitHub Issue #${evt.github_issue_number} ${res.deduplicated ? "(deduplicated)" : "created"}`
          : "";
        setBanner({
          kind: "info",
          text: `Parsed ${diffCount} upstream parameter diff(s) (${evt.base_sha.slice(0, 7)} → ${evt.head_sha.slice(0, 7)})${issueSuffix}. Candidate weights staged for 1-click activation.`,
        });
      } else if (evt.status === "ignored_non_ranking") {
        setBanner({
          kind: "info",
          text: evt.structural_summary,
        });
      } else {
        setBanner({
          kind: "success",
          text: `Upstream xai-org/x-algorithm matches active baseline ${res.last_synced_sha.slice(0, 7)}.`,
        });
      }
    } catch (err) {
      setBanner({
        kind: "error",
        text:
          err instanceof Error
            ? err.message
            : "Upstream x-algorithm check failed.",
      });
    } finally {
      setCheckingLive(false);
      setSimulating(false);
    }
  }

  async function handleActivateStaged(evt: UpstreamAlgoEventItem) {
    setActivatingId(evt.id);
    setBanner(null);
    try {
      const res = await risingApi.activateStagedWeights(evt.id, activeWorkspace?.id ?? null);
      await Promise.all([loadStatus(), refreshAll()]);
      setBanner({
        kind: "success",
        text: `Activated staged AlgoParamVersion #${res.activated_param_version?.version_number ?? ""} (@ ${res.last_synced_sha.slice(0, 7)}) across live tracer.`,
      });
    } catch (err) {
      setBanner({
        kind: "error",
        text:
          err instanceof Error
            ? err.message
            : "Failed to activate staged parameter version.",
      });
    } finally {
      setActivatingId(null);
    }
  }

  const syncedSha = statusData?.last_synced_sha || "9106e9f";
  const events = statusData?.events ?? [];

  return (
    <section className="rounded-2xl border border-slate-800 bg-surface-900/85 p-5 lg:p-6 space-y-5">
      {/* Header Row */}
      <div className="flex flex-col lg:flex-row lg:items-center justify-between gap-4">
        <div className="space-y-1">
          <div className="inline-flex items-center gap-2 text-xs font-medium text-sky-400">
            <GitBranch className="h-3.5 w-3.5" />
            <span>System Guardrail · Automated Upstream Algo Sync Watcher</span>
          </div>
          <h2 className="text-lg font-semibold text-white tracking-tight">
            xai-org/x-algorithm Drift &amp; Auto-Issue Monitor
          </h2>
          <p className="text-xs text-slate-400 max-w-2xl">
            Watches ranking-critical Rust &amp; Scala paths (<code>vm-ranker/</code>,{" "}
            <code>xai-value-model/</code>, <code>home-mixer/</code>,{" "}
            <code>user_cred_v2/</code>), parses <code>param!(...)</code> weight shifts into
            staged Backtester versions, and opens deduplicated{" "}
            <code>algo-sync</code> issues on{" "}
            <span className="text-slate-200 font-mono">
              {statusData?.target_github_repo || "gentoftech/tweettracker"}
            </span>
            .
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-2.5 shrink-0">
          <button
            type="button"
            disabled={simulating || checkingLive}
            onClick={() => void handleRunCheck(true)}
            className="inline-flex items-center gap-2 px-3.5 py-2 rounded-xl border border-amber-500/40 bg-amber-500/10 hover:bg-amber-500/20 text-amber-200 text-xs font-medium transition-colors disabled:opacity-50"
          >
            <FlaskConical className={`h-3.5 w-3.5 ${simulating ? "animate-spin" : ""}`} />
            <span>{simulating ? "Simulating..." : "Simulate Rust param!() Drift"}</span>
          </button>

          <button
            type="button"
            disabled={checkingLive || simulating}
            onClick={() => void handleRunCheck(false)}
            className="inline-flex items-center gap-2 px-3.5 py-2 rounded-xl bg-sky-500 hover:bg-sky-400 text-slate-950 text-xs font-semibold transition-colors disabled:opacity-50"
          >
            <Play className="h-3.5 w-3.5" />
            <span>
              {checkingLive ? "Checking Upstream..." : "Check Upstream x-algorithm Now"}
            </span>
          </button>

          <button
            type="button"
            onClick={() => void loadStatus()}
            className="p-2 rounded-xl border border-slate-700 bg-surface-850 hover:bg-surface-800 text-slate-300 transition-colors"
            title="Reload Sync History"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin text-sky-400" : ""}`} />
          </button>
        </div>
      </div>

      {/* Status Pills Strip */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
        <div className="rounded-xl border border-slate-800 bg-surface-850/80 px-3.5 py-2.5 flex items-center justify-between">
          <div>
            <div className="text-[11px] text-slate-400">Active Tracer Baseline SHA</div>
            <div className="text-sm font-mono font-semibold text-sky-300 mt-0.5 flex items-center gap-1.5">
              <GitCommit className="h-3.5 w-3.5 text-sky-400" />
              <span>{syncedSha.slice(0, 7)}</span>
            </div>
          </div>
          <a
            href={`https://github.com/${statusData?.upstream_repo || "xai-org/x-algorithm"}/commit/${syncedSha}`}
            target="_blank"
            rel="noreferrer"
            className="text-xs text-slate-400 hover:text-sky-300 inline-flex items-center gap-1"
          >
            <span>Upstream</span>
            <ExternalLink className="h-3 w-3" />
          </a>
        </div>

        <div className="rounded-xl border border-slate-800 bg-surface-850/80 px-3.5 py-2.5 flex items-center justify-between">
          <div>
            <div className="text-[11px] text-slate-400">GitHub Auto-Issues Target</div>
            <div className="text-xs font-mono text-slate-200 mt-0.5">
              {statusData?.target_github_repo || "gentoftech/tweettracker"}
            </div>
          </div>
          <span
            className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] font-medium border ${
              statusData?.github_token_configured
                ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-300"
                : "border-slate-700 bg-slate-800 text-slate-400"
            }`}
          >
            <ShieldCheck className="h-3 w-3" />
            <span>
              {statusData?.github_token_configured
                ? statusData.github_auth_source === "cli:gh_auth_token"
                  ? "gh CLI Ready"
                  : "GITHUB_TOKEN Set"
                : "Dry-Run (No Token)"}
            </span>
          </span>
        </div>

        <div className="rounded-xl border border-slate-800 bg-surface-850/80 px-3.5 py-2.5 flex items-center justify-between">
          <div>
            <div className="text-[11px] text-slate-400">Signal vs. Noise Path Guard</div>
            <div className="text-xs text-slate-200 mt-0.5">
              {statusData?.watched_paths.length ?? 11} ranking paths · ignores <code>ads/</code>
            </div>
          </div>
          <button
            type="button"
            onClick={() => setShowWatchedPaths((p) => !p)}
            className="text-[11px] text-sky-400 hover:underline"
          >
            {showWatchedPaths ? "Hide Paths" : "View Paths"}
          </button>
        </div>
      </div>

      {showWatchedPaths && statusData && (
        <div className="rounded-xl border border-slate-800 bg-surface-950/70 p-3.5 text-xs space-y-2">
          <div className="text-slate-300 font-medium">Watched Upstream Ranking Paths:</div>
          <div className="flex flex-wrap gap-1.5">
            {statusData.watched_paths.map((p) => (
              <code
                key={p}
                className="px-2 py-0.5 rounded bg-surface-850 border border-slate-800 text-sky-300 text-[11px]"
              >
                {p}
              </code>
            ))}
          </div>
          <div className="text-slate-400 text-[11px] pt-1">
            Explicitly ignored non-ranking churn:{" "}
            {statusData.ignored_paths.map((ip) => (
              <code key={ip} className="mr-1.5 text-slate-300">
                {ip}
              </code>
            ))}
          </div>
        </div>
      )}

      {banner && (
        <div
          className={`rounded-xl border px-4 py-3 text-xs flex items-start gap-2.5 ${
            banner.kind === "error"
              ? "border-rose-500/40 bg-rose-500/10 text-rose-200"
              : banner.kind === "success"
                ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-200"
                : "border-sky-500/40 bg-sky-500/10 text-sky-200"
          }`}
        >
          {banner.kind === "error" ? (
            <AlertTriangle className="h-4 w-4 shrink-0 mt-0.5 text-rose-400" />
          ) : (
            <CheckCircle2 className="h-4 w-4 shrink-0 mt-0.5 text-emerald-400" />
          )}
          <span>{banner.text}</span>
        </div>
      )}

      {/* Upstream Sync Event History */}
      <div className="space-y-3">
        <div className="text-xs font-semibold uppercase tracking-wider text-slate-400">
          Upstream Sync Event Log ({events.length})
        </div>

        {events.length === 0 ? (
          <div className="rounded-xl border border-slate-800 bg-surface-850/50 p-6 text-center text-xs text-slate-400">
            No upstream sync checks recorded yet. Click{" "}
            <strong className="text-slate-200">Simulate Rust param!() Drift</strong> or{" "}
            <strong className="text-slate-200">Check Upstream x-algorithm Now</strong> to test.
          </div>
        ) : (
          <div className="space-y-3">
            {events.map((evt) => {
              const meta = statusBadgeMeta(evt.status);
              const staged = evt.staged_param_version;
              const canActivate =
                Boolean(evt.staged_param_version_id) &&
                (!staged || !staged.is_active) &&
                evt.status !== "in_sync";

              return (
                <div
                  key={evt.id}
                  className="rounded-xl border border-slate-800 bg-surface-850/60 p-4 space-y-3"
                >
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div className="flex flex-wrap items-center gap-2.5">
                      <span
                        className={`px-2.5 py-0.5 rounded-full text-[11px] font-medium border ${meta.tone}`}
                      >
                        {meta.label}
                      </span>
                      <a
                        href={`https://github.com/${statusData?.upstream_repo || "xai-org/x-algorithm"}/compare/${evt.base_sha}...${evt.head_sha}`}
                        target="_blank"
                        rel="noreferrer"
                        className="font-mono text-xs text-slate-200 hover:text-sky-300 inline-flex items-center gap-1"
                      >
                        <span>
                          {evt.base_sha.slice(0, 7)} → {evt.head_sha.slice(0, 7)}
                        </span>
                        <ExternalLink className="h-3 w-3 text-slate-400" />
                      </a>

                      {evt.github_issue_url && (
                        <a
                          href={evt.github_issue_url}
                          target="_blank"
                          rel="noreferrer"
                          className="inline-flex items-center gap-1 px-2 py-0.5 rounded-md bg-violet-500/15 border border-violet-500/30 text-violet-300 text-[11px] font-mono hover:bg-violet-500/25"
                        >
                          <span>Issue #{evt.github_issue_number ?? "open"}</span>
                          <ExternalLink className="h-2.5 w-2.5" />
                        </a>
                      )}

                      {staged && (
                        <span className="text-[11px] font-mono px-2 py-0.5 rounded bg-slate-800 text-slate-300 border border-slate-700">
                          v{staged.version_number} ({staged.is_active ? "Active" : "Staged"})
                        </span>
                      )}
                    </div>

                    <div className="flex items-center gap-3">
                      {evt.detected_at && (
                        <span className="text-[11px] text-slate-400">
                          {new Date(evt.detected_at).toLocaleString()}
                        </span>
                      )}
                      {canActivate && (
                        <button
                          type="button"
                          disabled={activatingId === evt.id}
                          onClick={() => void handleActivateStaged(evt)}
                          className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-emerald-500/20 hover:bg-emerald-500/30 border border-emerald-500/40 text-emerald-200 text-xs font-medium transition-colors disabled:opacity-50"
                        >
                          <Zap className="h-3.5 w-3.5 text-emerald-400" />
                          <span>
                            {activatingId === evt.id
                              ? "Activating..."
                              : "Activate Staged Weights"}
                          </span>
                        </button>
                      )}
                    </div>
                  </div>

                  <p className="text-xs text-slate-300 leading-relaxed">
                    {evt.structural_summary}
                  </p>

                  {evt.param_diffs_json.length > 0 && (
                    <div className="overflow-x-auto rounded-lg border border-slate-800 bg-surface-950/70">
                      <table className="w-full text-left text-xs">
                        <thead className="border-b border-slate-800 text-[11px] uppercase text-slate-400 bg-surface-900/80">
                          <tr>
                            <th className="px-3 py-2 font-medium">Rust Identifier</th>
                            <th className="px-3 py-2 font-medium">Tracer Field</th>
                            <th className="px-3 py-2 font-medium">File</th>
                            <th className="px-3 py-2 font-medium text-right">Before → After</th>
                            <th className="px-3 py-2 font-medium text-right">Δ%</th>
                          </tr>
                        </thead>
                        <tbody className="divide-y divide-slate-800/70 font-mono">
                          {evt.param_diffs_json.map((diff, dIdx) => (
                            <tr key={`${diff.rust_ident}-${dIdx}`}>
                              <td className="px-3 py-2 text-sky-300">{diff.rust_ident}</td>
                              <td className="px-3 py-2 text-slate-300">{diff.mapped_field}</td>
                              <td className="px-3 py-2 text-slate-400 text-[11px]">{diff.file}</td>
                              <td className="px-3 py-2 text-right">
                                <span className="text-rose-300/90">
                                  {String(diff.old_value)}
                                </span>
                                <span className="text-slate-500 mx-1.5">→</span>
                                <span className="text-emerald-300 font-semibold">
                                  {String(diff.new_value)}
                                </span>
                              </td>
                              <td className="px-3 py-2 text-right text-[11px]">
                                {diff.pct_change != null ? (
                                  <span
                                    className={
                                      diff.pct_change >= 0
                                        ? "text-emerald-300"
                                        : "text-amber-300"
                                    }
                                  >
                                    {diff.pct_change >= 0 ? "+" : ""}
                                    {diff.pct_change.toFixed(1)}%
                                  </span>
                                ) : (
                                  <span className="text-slate-500">—</span>
                                )}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>
    </section>
  );
}
