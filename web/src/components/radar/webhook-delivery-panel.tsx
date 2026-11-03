"use client";

import React, { useCallback, useEffect, useState } from "react";
import {
  CheckCircle2,
  Clock,
  KeyRound,
  Plus,
  RefreshCw,
  Send,
  ShieldAlert,
  Trash2,
  Webhook,
} from "lucide-react";
import { useWorkspace } from "@/components/workspace-context";
import { radarApi } from "@/lib/radar-api";
import type {
  AlertDeliveryItem,
  AlertsListResponse,
  WebhookTestResponse,
} from "@/lib/radar-types";

interface WebhookDeliveryPanelProps {
  workspaceId?: number;
  defaultExpanded?: boolean;
}

export function WebhookDeliveryPanel({
  workspaceId: propWorkspaceId,
  defaultExpanded = false,
}: WebhookDeliveryPanelProps) {
  const { activeWorkspace, updateWorkspace } = useWorkspace();
  const effectiveWorkspaceId = propWorkspaceId ?? activeWorkspace?.id ?? null;

  const [expanded, setExpanded] = useState<boolean>(defaultExpanded);
  const [alertsMeta, setAlertsMeta] = useState<AlertsListResponse | null>(null);
  const [channelFilter, setChannelFilter] = useState<"all" | "webhook" | "sse">("all");
  const [newUrlInput, setNewUrlInput] = useState<string>("");
  const [savingUrl, setSavingUrl] = useState<boolean>(false);
  const [testingWebhook, setTestingWebhook] = useState<boolean>(false);
  const [lastTestResult, setLastTestResult] = useState<WebhookTestResponse | null>(
    null
  );
  const [panelError, setPanelError] = useState<string | null>(null);
  const [loadingLogs, setLoadingLogs] = useState<boolean>(false);

  const fetchDeliveries = useCallback(async () => {
    if (!effectiveWorkspaceId) return;
    setLoadingLogs(true);
    try {
      const res = await radarApi.listAlerts(effectiveWorkspaceId, {
        channel: channelFilter === "all" ? undefined : channelFilter,
        limit: 30,
      });
      setAlertsMeta(res);
    } catch (err) {
      setPanelError(
        err instanceof Error ? err.message : "Failed to load alert delivery logs."
      );
    } finally {
      setLoadingLogs(false);
    }
  }, [effectiveWorkspaceId, channelFilter]);

  useEffect(() => {
    if (expanded && effectiveWorkspaceId) {
      void fetchDeliveries();
    }
  }, [expanded, effectiveWorkspaceId, fetchDeliveries]);

  if (!effectiveWorkspaceId || !activeWorkspace) {
    return null;
  }

  const configuredUrls = activeWorkspace.webhook_urls_json ?? [];

  const handleAddWebhookUrl = async (e: React.FormEvent) => {
    e.preventDefault();
    const trimmed = newUrlInput.trim();
    if (!trimmed) return;
    if (!trimmed.startsWith("https://")) {
      setPanelError("Webhook URL must start with https://");
      return;
    }
    if (configuredUrls.includes(trimmed)) {
      setNewUrlInput("");
      return;
    }

    setSavingUrl(true);
    setPanelError(null);
    try {
      await updateWorkspace({
        webhook_urls_json: [...configuredUrls, trimmed],
      });
      setNewUrlInput("");
      await fetchDeliveries();
    } catch (err) {
      setPanelError(
        err instanceof Error ? err.message : "Failed to save webhook URL."
      );
    } finally {
      setSavingUrl(false);
    }
  };

  const handleRemoveWebhookUrl = async (urlToRemove: string) => {
    setSavingUrl(true);
    setPanelError(null);
    try {
      await updateWorkspace({
        webhook_urls_json: configuredUrls.filter((u) => u !== urlToRemove),
      });
      await fetchDeliveries();
    } catch (err) {
      setPanelError(
        err instanceof Error ? err.message : "Failed to remove webhook URL."
      );
    } finally {
      setSavingUrl(false);
    }
  };

  const handleSendTestWebhook = async () => {
    setTestingWebhook(true);
    setPanelError(null);
    try {
      const adhocTarget = newUrlInput.trim() ? newUrlInput.trim() : null;
      const res = await radarApi.sendTestWebhook(effectiveWorkspaceId, {
        target_url: adhocTarget,
        note: "Manual HMAC-SHA256 verification ping from TweetTracker Radar",
      });
      setLastTestResult(res);
      await fetchDeliveries();
    } catch (err) {
      setPanelError(
        err instanceof Error ? err.message : "Webhook test dispatch failed."
      );
    } finally {
      setTestingWebhook(false);
    }
  };

  return (
    <section className="rounded-2xl border border-slate-800 bg-surface-900/80 overflow-hidden">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="w-full px-5 py-3.5 flex flex-wrap items-center justify-between gap-3 text-left hover:bg-surface-850/60 transition-colors"
      >
        <div className="flex items-center gap-2.5">
          <Webhook className="h-4 w-4 text-sky-400" />
          <span className="text-sm font-semibold text-white">
            Outbound HMAC-SHA256 Webhooks &amp; Delivery Audit Log
          </span>
          <span className="px-2 py-0.5 rounded-full bg-surface-950 border border-slate-800 text-[11px] font-mono text-slate-300">
            {configuredUrls.length} endpoint{configuredUrls.length === 1 ? "" : "s"}
          </span>
        </div>
        <div className="flex items-center gap-3 text-xs text-slate-400">
          <span className="inline-flex items-center gap-1 font-mono">
            <Clock className="h-3.5 w-3.5 text-slate-500" />
            Cap: {activeWorkspace.max_alerts_per_hour}/hr
            {activeWorkspace.quiet_hours_start && activeWorkspace.quiet_hours_end
              ? ` · Quiet ${activeWorkspace.quiet_hours_start}–${activeWorkspace.quiet_hours_end} UTC`
              : ""}
          </span>
          <span className="text-sky-400 font-medium">
            {expanded ? "Collapse" : "Configure & Test"}
          </span>
        </div>
      </button>

      {expanded && (
        <div className="px-5 pb-5 pt-2 border-t border-slate-800/80 space-y-4">
          {panelError && (
            <div className="rounded-xl border border-rose-500/40 bg-rose-950/30 px-3.5 py-2.5 text-xs text-rose-200 flex items-center gap-2">
              <ShieldAlert className="h-4 w-4 text-rose-400 shrink-0" />
              <span>{panelError}</span>
            </div>
          )}

          {/* Signing Status + URL Configuration */}
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            <div className="rounded-xl bg-surface-950/80 border border-slate-800 p-4 space-y-3">
              <div className="flex items-center justify-between gap-2">
                <span className="text-xs font-semibold text-white">
                  Workspace Webhook Endpoints
                </span>
                <span
                  className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-[10px] font-mono border ${
                    alertsMeta?.webhook_secret_configured
                      ? "bg-emerald-500/10 border-emerald-500/30 text-emerald-300"
                      : "bg-amber-500/10 border-amber-500/30 text-amber-300"
                  }`}
                >
                  <KeyRound className="h-3 w-3" />
                  {alertsMeta?.webhook_secret_configured
                    ? "TWEETTRACKER_WEBHOOK_SECRET Set"
                    : "Unsigned (Secret Unset)"}
                </span>
              </div>

              <form onSubmit={handleAddWebhookUrl} className="flex gap-2">
                <input
                  type="url"
                  value={newUrlInput}
                  onChange={(e) => setNewUrlInput(e.target.value)}
                  placeholder="https://hooks.example.com/tweettracker"
                  className="flex-1 rounded-lg bg-surface-900 border border-slate-700 px-3 py-1.5 text-xs text-white placeholder:text-slate-500 focus:outline-none focus:border-sky-500 font-mono"
                />
                <button
                  type="submit"
                  disabled={savingUrl || !newUrlInput.trim()}
                  className="inline-flex items-center gap-1 px-3 py-1.5 rounded-lg bg-sky-500/20 hover:bg-sky-500/30 disabled:opacity-50 border border-sky-500/40 text-xs font-medium text-sky-200"
                >
                  <Plus className="h-3.5 w-3.5" />
                  <span>Add</span>
                </button>
              </form>

              {configuredUrls.length === 0 ? (
                <p className="text-xs text-slate-400">
                  No outbound webhook URLs saved yet. Add a URL above (or enter one and click{" "}
                  <strong>Send Test Payload</strong>).
                </p>
              ) : (
                <ul className="space-y-1.5">
                  {configuredUrls.map((url) => (
                    <li
                      key={url}
                      className="flex items-center justify-between gap-2 rounded-lg bg-surface-900 border border-slate-800 px-3 py-1.5 text-xs font-mono text-slate-200"
                    >
                      <span className="truncate">{url}</span>
                      <button
                        type="button"
                        disabled={savingUrl}
                        onClick={() => void handleRemoveWebhookUrl(url)}
                        className="text-slate-400 hover:text-rose-400 shrink-0"
                        title="Remove webhook endpoint"
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </div>

            {/* HMAC Signature Verification & 1-Click Test */}
            <div className="rounded-xl bg-surface-950/80 border border-slate-800 p-4 flex flex-col justify-between gap-3">
              <div className="space-y-1.5">
                <div className="text-xs font-semibold text-white">
                  HMAC-SHA256 Payload Signing &amp; Deduplication Rules
                </div>
                <p className="text-xs text-slate-400 leading-relaxed">
                  Every outbound request includes{" "}
                  <code className="text-sky-300 font-mono">
                    X-TweetTracker-Signature-256: sha256=&lt;hex&gt;
                  </code>{" "}
                  computed over the canonical JSON body. Watch alerts never re-fire for the
                  same post; upgrading from <strong>Watch &rarr; Surge</strong> dispatches
                  once, respecting overnight quiet hours and the{" "}
                  <strong>{activeWorkspace.max_alerts_per_hour}/hr</strong> rate cap.
                </p>
              </div>

              <div className="flex items-center justify-between gap-3 flex-wrap pt-1">
                <button
                  type="button"
                  disabled={
                    testingWebhook ||
                    (configuredUrls.length === 0 && !newUrlInput.trim())
                  }
                  onClick={() => void handleSendTestWebhook()}
                  className="inline-flex items-center gap-1.5 px-3.5 py-2 rounded-lg bg-emerald-500/20 hover:bg-emerald-500/30 disabled:opacity-50 border border-emerald-500/40 text-xs font-medium text-emerald-200 transition-colors"
                >
                  <Send className="h-3.5 w-3.5" />
                  <span>
                    {testingWebhook
                      ? "Dispatching Signed Test..."
                      : "Send Test HMAC Webhook"}
                  </span>
                </button>

                {lastTestResult && (
                  <div className="text-[11px] font-mono text-slate-300 truncate max-w-xs">
                    Sig:{" "}
                    <span className="text-emerald-300">
                      {lastTestResult.signature_preview}
                    </span>
                  </div>
                )}
              </div>
            </div>
          </div>

          {/* Delivery Audit Table */}
          <div className="space-y-2">
            <div className="flex items-center justify-between gap-2 flex-wrap">
              <div className="flex items-center gap-1.5">
                {(["all", "webhook", "sse"] as const).map((ch) => (
                  <button
                    key={ch}
                    type="button"
                    onClick={() => setChannelFilter(ch)}
                    className={`px-2.5 py-1 rounded-lg text-xs font-medium border transition-colors ${
                      channelFilter === ch
                        ? "bg-sky-500/20 border-sky-500/40 text-sky-200"
                        : "bg-surface-950 border-slate-800 text-slate-400 hover:text-slate-200"
                    }`}
                  >
                    {ch === "all" ? "All Channels" : ch.toUpperCase()}
                  </button>
                ))}
              </div>
              <button
                type="button"
                onClick={() => void fetchDeliveries()}
                className="inline-flex items-center gap-1 text-xs text-slate-400 hover:text-white"
              >
                <RefreshCw
                  className={`h-3.5 w-3.5 ${loadingLogs ? "animate-spin" : ""}`}
                />
                <span>Refresh Log</span>
              </button>
            </div>

            <div className="rounded-xl border border-slate-800 overflow-x-auto">
              <table className="w-full text-left text-xs">
                <thead className="bg-surface-950 text-slate-400 border-b border-slate-800">
                  <tr>
                    <th className="px-3 py-2 font-medium">Time</th>
                    <th className="px-3 py-2 font-medium">Channel</th>
                    <th className="px-3 py-2 font-medium">Event / Tier</th>
                    <th className="px-3 py-2 font-medium">Target / Post</th>
                    <th className="px-3 py-2 font-medium">Status</th>
                    <th className="px-3 py-2 font-medium text-right">Attempts</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-800/70">
                  {(alertsMeta?.items ?? []).length === 0 ? (
                    <tr>
                      <td
                        colSpan={6}
                        className="px-4 py-6 text-center text-slate-400"
                      >
                        No alert delivery entries match this filter.
                      </td>
                    </tr>
                  ) : (
                    (alertsMeta?.items ?? []).slice(0, 15).map((row: AlertDeliveryItem) => (
                      <tr key={row.id} className="bg-surface-900/60">
                        <td className="px-3 py-2 font-mono text-[11px] text-slate-400 whitespace-nowrap">
                          {new Date(row.created_at).toLocaleTimeString()}
                        </td>
                        <td className="px-3 py-2 font-mono uppercase text-[11px] text-slate-300">
                          {row.channel}
                        </td>
                        <td className="px-3 py-2 whitespace-nowrap">
                          <span className="font-mono text-slate-200">
                            {row.event_type}
                          </span>{" "}
                          <span
                            className={`ml-1 px-1.5 py-0.5 rounded text-[10px] font-semibold uppercase ${
                              row.alert_tier === "surge"
                                ? "bg-rose-500/20 text-rose-300"
                                : row.alert_tier === "watch"
                                ? "bg-amber-500/20 text-amber-300"
                                : "bg-sky-500/20 text-sky-300"
                            }`}
                          >
                            {row.alert_tier}
                          </span>
                        </td>
                        <td className="px-3 py-2 font-mono text-[11px] text-slate-400 max-w-xs truncate">
                          {row.target_url ||
                            (row.post_id ? `Post #${row.post_id}` : "SSE Stream")}
                        </td>
                        <td className="px-3 py-2 whitespace-nowrap font-mono text-[11px]">
                          <span
                            className={`inline-flex items-center gap-1 ${
                              row.status === "delivered"
                                ? "text-emerald-400"
                                : row.status.startsWith("suppressed")
                                ? "text-amber-400"
                                : "text-rose-400"
                            }`}
                          >
                            {row.status === "delivered" && (
                              <CheckCircle2 className="h-3 w-3" />
                            )}
                            {row.status}
                            {row.http_status ? ` (${row.http_status})` : ""}
                          </span>
                        </td>
                        <td className="px-3 py-2 text-right font-mono text-slate-300">
                          {row.attempts}
                        </td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
