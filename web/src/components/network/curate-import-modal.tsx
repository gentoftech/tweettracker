"use client";

import React, { useEffect, useState } from "react";
import {
  FileSpreadsheet,
  ListChecks,
  Loader2,
  RefreshCw,
  Upload,
  UserPlus,
  X,
} from "lucide-react";
import { api } from "@/lib/api";
import { useWorkspace } from "@/components/workspace-context";
import type { AccountImportResult } from "@/lib/types";

export type ImportModalTab = "handles" | "csv" | "x_list";

interface CurateImportModalProps {
  open: boolean;
  initialTab?: ImportModalTab;
  onClose: () => void;
  onCompleted: () => Promise<void> | void;
}

export function CurateImportModal({
  open,
  initialTab = "handles",
  onClose,
  onCompleted,
}: CurateImportModalProps) {
  const { activeWorkspace, refreshAll } = useWorkspace();

  const [tab, setTab] = useState<ImportModalTab>(initialTab);
  const [handlesText, setHandlesText] = useState<string>("");
  const [csvText, setCsvText] = useState<string>("");
  const [xListId, setXListId] = useState<string>("");

  const [submitting, setSubmitting] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [resultBanner, setResultBanner] = useState<string | null>(null);

  useEffect(() => {
    if (open) {
      setTab(initialTab);
      setError(null);
      setResultBanner(null);
    }
  }, [open, initialTab, activeWorkspace?.id]);

  if (!open || !activeWorkspace) return null;

  const savedListIds = activeWorkspace.x_list_ids_json ?? [];

  const handleFileUpload = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      if (typeof reader.result === "string") {
        setCsvText(reader.result);
      }
    };
    reader.readAsText(file);
  };

  const formatImportBanner = (res: AccountImportResult): string =>
    `Imported ${res.imported_count} new · Updated ${res.updated_existing_count} existing · Deduplicated ${res.deduplicated_count} (${res.total_tracked_accounts} tracked).`;

  const handleImportHandles = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!handlesText.trim()) {
      setError("Paste at least one @handle or https://x.com/... profile URL.");
      return;
    }
    setSubmitting(true);
    setError(null);
    setResultBanner(null);
    try {
      const res = await api.importNetworkAccounts(activeWorkspace.id, {
        handles_text: handlesText,
        recompute: true,
      });
      setHandlesText("");
      setResultBanner(formatImportBanner(res));
      await refreshAll();
      await onCompleted();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Account import failed.");
    } finally {
      setSubmitting(false);
    }
  };

  const handleImportCsv = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!csvText.trim()) {
      setError("Paste CSV rows or upload a .csv file first.");
      return;
    }
    setSubmitting(true);
    setError(null);
    setResultBanner(null);
    try {
      const res = await api.importNetworkAccounts(activeWorkspace.id, {
        csv_text: csvText,
        recompute: true,
      });
      setCsvText("");
      setResultBanner(formatImportBanner(res));
      await refreshAll();
      await onCompleted();
    } catch (err) {
      setError(err instanceof Error ? err.message : "CSV import failed.");
    } finally {
      setSubmitting(false);
    }
  };

  const handleSyncXList = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!xListId.trim()) {
      setError("Enter an X List ID or x.com/i/lists/... URL.");
      return;
    }
    setSubmitting(true);
    setError(null);
    setResultBanner(null);
    try {
      const res = await api.importNetworkAccounts(activeWorkspace.id, {
        x_list_id: xListId.trim(),
        recompute: true,
      });
      setResultBanner(formatImportBanner(res));
      await refreshAll();
      await onCompleted();
    } catch (err) {
      setError(err instanceof Error ? err.message : "X List sync failed.");
    } finally {
      setSubmitting(false);
    }
  };

  const handleManualRecompute = async () => {
    setSubmitting(true);
    setError(null);
    try {
      const res = await api.recomputeNetwork(activeWorkspace.id);
      setResultBanner(
        `Recomputed ${res.accounts_count} people and ${res.edges_count} connections in ${res.compute_duration_ms.toFixed(1)} ms.`
      );
      await refreshAll();
      await onCompleted();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Graph recompute failed.");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/65 p-4 backdrop-blur-xs">
      <div
        className="flex max-h-[88vh] w-full max-w-xl flex-col overflow-hidden rounded-2xl border border-slate-800 bg-surface-900 shadow-2xl"
        role="dialog"
        aria-label="Add People"
      >
        {/* Header */}
        <div className="flex items-center justify-between border-b border-slate-800 px-6 py-4">
          <div>
            <h2 className="text-base font-semibold text-white">Add People</h2>
            <p className="mt-0.5 text-xs text-slate-400">
              Import seed accounts or sync an X List—clusters form automatically from real interactions.
            </p>
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              disabled={submitting}
              onClick={() => void handleManualRecompute()}
              className="inline-flex items-center gap-1.5 rounded-lg border border-slate-700 bg-slate-800/80 px-2.5 py-1.5 text-xs font-medium text-slate-200 transition hover:bg-slate-700"
            >
              <RefreshCw
                className={`h-3.5 w-3.5 ${submitting ? "animate-spin" : ""}`}
              />
              Recompute
            </button>
            <button
              type="button"
              onClick={onClose}
              className="rounded-lg p-1.5 text-slate-400 hover:bg-slate-800 hover:text-white"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        </div>

        {/* Mode Tabs */}
        <div className="flex border-b border-slate-800 bg-surface-950/50 px-6">
          {[
            { id: "handles", label: "@Handles / URLs", icon: UserPlus },
            { id: "csv", label: "CSV Import", icon: FileSpreadsheet },
            { id: "x_list", label: "Sync X List", icon: ListChecks },
          ].map((item) => {
            const Icon = item.icon;
            const active = tab === item.id;
            return (
              <button
                key={item.id}
                type="button"
                onClick={() => {
                  setTab(item.id as ImportModalTab);
                  setError(null);
                }}
                className={`flex items-center gap-1.5 border-b-2 px-3.5 py-3 text-xs font-medium transition ${
                  active
                    ? "border-sky-400 text-sky-300"
                    : "border-transparent text-slate-400 hover:text-slate-200"
                }`}
              >
                <Icon className="h-3.5 w-3.5" />
                {item.label}
              </button>
            );
          })}
        </div>

        {/* Body */}
        <div className="flex-1 space-y-4 overflow-y-auto px-6 py-5">
          {error && (
            <div className="rounded-lg border border-rose-500/40 bg-rose-500/10 px-3.5 py-2.5 text-xs text-rose-300">
              {error}
            </div>
          )}
          {resultBanner && (
            <div className="rounded-lg border border-emerald-500/40 bg-emerald-500/10 px-3.5 py-2.5 text-xs text-emerald-300">
              {resultBanner}
            </div>
          )}

          {tab === "handles" && (
            <form onSubmit={(e) => void handleImportHandles(e)} className="space-y-4">
              <div>
                <label className="block text-xs font-medium text-slate-300">
                  Paste @handles or X profile URLs (one per line or comma-separated)
                </label>
                <textarea
                  rows={5}
                  value={handlesText}
                  onChange={(e) => setHandlesText(e.target.value)}
                  placeholder={`@example_voice_1\nhttps://x.com/example_voice_2\n@example_voice_3, @example_voice_4`}
                  className="mt-1.5 w-full rounded-xl border border-slate-800 bg-surface-950 px-3.5 py-2.5 font-mono text-xs text-slate-100 placeholder:text-slate-600 focus:border-sky-500 focus:outline-none"
                />
              </div>
              <div className="flex justify-end">
                <button
                  type="submit"
                  disabled={submitting}
                  className="inline-flex items-center gap-2 rounded-lg bg-sky-500 px-4 py-2 text-xs font-semibold text-slate-950 transition hover:bg-sky-400 disabled:opacity-50"
                >
                  {submitting ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  ) : (
                    <UserPlus className="h-3.5 w-3.5" />
                  )}
                  Import People
                </button>
              </div>
            </form>
          )}

          {tab === "csv" && (
            <form onSubmit={(e) => void handleImportCsv(e)} className="space-y-4">
              <div className="flex items-center justify-between">
                <label className="block text-xs font-medium text-slate-300">
                  CSV Handles (one <code className="text-sky-300">handle</code> or X URL per row)
                </label>
                <label className="inline-flex cursor-pointer items-center gap-1.5 rounded-lg border border-slate-700 bg-slate-800/80 px-2.5 py-1 text-[11px] font-medium text-slate-200 hover:bg-slate-700">
                  <Upload className="h-3 w-3" />
                  Load .csv File
                  <input
                    type="file"
                    accept=".csv,text/csv,text/plain"
                    onChange={handleFileUpload}
                    className="hidden"
                  />
                </label>
              </div>
              <textarea
                rows={5}
                value={csvText}
                onChange={(e) => setCsvText(e.target.value)}
                placeholder={`handle\n@example_voice_1\nhttps://x.com/example_voice_2`}
                className="w-full rounded-xl border border-slate-800 bg-surface-950 px-3.5 py-2.5 font-mono text-xs text-slate-100 placeholder:text-slate-600 focus:border-sky-500 focus:outline-none"
              />
              <div className="flex justify-end">
                <button
                  type="submit"
                  disabled={submitting}
                  className="inline-flex items-center gap-2 rounded-lg bg-sky-500 px-4 py-2 text-xs font-semibold text-slate-950 transition hover:bg-sky-400 disabled:opacity-50"
                >
                  {submitting ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  ) : (
                    <FileSpreadsheet className="h-3.5 w-3.5" />
                  )}
                  Import CSV
                </button>
              </div>
            </form>
          )}

          {tab === "x_list" && (
            <form onSubmit={(e) => void handleSyncXList(e)} className="space-y-4">
              <div>
                <label className="block text-xs font-medium text-slate-300">
                  X List ID or URL (synced via{" "}
                  <span className="font-mono text-sky-300">
                    {activeWorkspace.x_data_provider}
                  </span>
                  )
                </label>
                <input
                  type="text"
                  value={xListId}
                  onChange={(e) => setXListId(e.target.value)}
                  placeholder="e.g. 17492019482710 or https://x.com/i/lists/17492019482710"
                  className="mt-1.5 w-full rounded-xl border border-slate-800 bg-surface-950 px-3.5 py-2 font-mono text-xs text-slate-100 placeholder:text-slate-600 focus:border-sky-500 focus:outline-none"
                />
              </div>

              {savedListIds.length > 0 && (
                <div className="flex flex-wrap items-center gap-2 text-[11px] text-slate-400">
                  <span>Saved workspace X Lists:</span>
                  {savedListIds.map((lid) => (
                    <button
                      key={lid}
                      type="button"
                      onClick={() => setXListId(lid)}
                      className="rounded border border-slate-700 bg-slate-800 px-2 py-0.5 font-mono text-sky-300 hover:border-sky-500/50"
                    >
                      {lid}
                    </button>
                  ))}
                </div>
              )}

              <div className="flex justify-end">
                <button
                  type="submit"
                  disabled={submitting}
                  className="inline-flex items-center gap-2 rounded-lg bg-sky-500 px-4 py-2 text-xs font-semibold text-slate-950 transition hover:bg-sky-400 disabled:opacity-50"
                >
                  {submitting ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  ) : (
                    <ListChecks className="h-3.5 w-3.5" />
                  )}
                  Sync X List
                </button>
              </div>
            </form>
          )}
        </div>
      </div>
    </div>
  );
}
