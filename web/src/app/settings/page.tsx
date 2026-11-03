"use client";

import React, { useCallback, useEffect, useState } from "react";
import {
  Bell,
  CheckCircle2,
  Database,
  KeyRound,
  Layers,
  Lock,
  Plus,
  RefreshCw,
  RotateCcw,
  Save,
  Sparkles,
  Trash2,
  Wallet,
  Webhook,
} from "lucide-react";
import { useWorkspace } from "@/components/workspace-context";
import { WebhookDeliveryPanel } from "@/components/radar/webhook-delivery-panel";
import { AlgoSyncPanel } from "@/components/rising/algo-sync-panel";
import { api } from "@/lib/api";
import { backtestApi } from "@/lib/backtest-api";
import type { ChangelogResponse, ScorecardResponse } from "@/lib/backtest-types";
import type { LLMProviderName, SecretStatus, XDataProviderName } from "@/lib/types";

export default function SettingsPage() {
  const {
    activeWorkspace,
    spendSummary,
    loading,
    authRequired,
    isAuthenticated,
    setShowNewWorkspaceModal,
    refreshAll,
    updateWorkspace,
    deleteWorkspace,
    reconcileSpend,
    clearAdminSession,
  } = useWorkspace();

  const [name, setName] = useState("");
  const [myHandle, setMyHandle] = useState("");
  const [xProvider, setXProvider] = useState<XDataProviderName>("fixture");
  const [llmProvider, setLlmProvider] = useState<LLMProviderName>("google_gemini");
  const [pollMinutes, setPollMinutes] = useState(10);
  const [useXLists, setUseXLists] = useState(false);
  const [xListIdsText, setXListIdsText] = useState("");
  const [monthlyUnitBudget, setMonthlyUnitBudget] = useState(100_000);
  const [hotPostsMinViews, setHotPostsMinViews] = useState(5_000);
  const [dailyCap, setDailyCap] = useState(2.0);
  const [monthlyCap, setMonthlyCap] = useState(25.0);
  const [watchThreshold, setWatchThreshold] = useState(65.0);
  const [surgeThreshold, setSurgeThreshold] = useState(82.0);
  const [quietStart, setQuietStart] = useState("");
  const [quietEnd, setQuietEnd] = useState("");
  const [maxAlertsHour, setMaxAlertsHour] = useState(10);
  const [webhookUrls, setWebhookUrls] = useState<string[]>([]);
  const [newWebhookUrl, setNewWebhookUrl] = useState("");

  // Zero-touch Model Accuracy & Auto-Tuning state
  const [scorecard, setScorecard] = useState<ScorecardResponse | null>(null);
  const [changelog, setChangelog] = useState<ChangelogResponse | null>(null);
  const [revertingWeights, setRevertingWeights] = useState(false);

  // Encrypted In-App API Keys state
  const [secretsList, setSecretsList] = useState<SecretStatus[]>([]);
  const [secretInputs, setSecretInputs] = useState<Record<string, string>>({});
  const [savingSecrets, setSavingSecrets] = useState(false);

  const [saving, setSaving] = useState(false);
  const [reconciling, setReconciling] = useState(false);
  const [banner, setBanner] = useState<{ kind: "ok" | "err"; text: string } | null>(
    null
  );

  const loadSecrets = useCallback(async () => {
    if (authRequired && !isAuthenticated) return;
    try {
      const res = await api.getSecrets();
      setSecretsList(res.secrets);
    } catch {
      // Non-fatal if locked or offline
    }
  }, [authRequired, isAuthenticated]);

  useEffect(() => {
    void loadSecrets();
  }, [loadSecrets]);

  async function handleSaveSecrets(e: React.FormEvent) {
    e.preventDefault();
    const payload: Record<string, string> = {};
    for (const [k, v] of Object.entries(secretInputs)) {
      if (v.trim()) {
        payload[k] = v.trim();
      }
    }
    if (Object.keys(payload).length === 0) return;

    setSavingSecrets(true);
    setBanner(null);
    try {
      const res = await api.updateSecrets(payload);
      setSecretsList(res.secrets);
      setSecretInputs({});
      await refreshAll();
      setBanner({
        kind: "ok",
        text: "API keys encrypted and saved.",
      });
    } catch (err) {
      setBanner({
        kind: "err",
        text: err instanceof Error ? err.message : "Failed to save API keys.",
      });
    } finally {
      setSavingSecrets(false);
    }
  }

  async function handleClearSecret(keyName: string) {
    setSavingSecrets(true);
    setBanner(null);
    try {
      const res = await api.updateSecrets({ [keyName]: "" });
      setSecretsList(res.secrets);
      setSecretInputs((prev) => {
        const next = { ...prev };
        delete next[keyName];
        return next;
      });
      await refreshAll();
      setBanner({
        kind: "ok",
        text: `Removed in-app key for ${keyName}.`,
      });
    } catch (err) {
      setBanner({
        kind: "err",
        text: err instanceof Error ? err.message : "Failed to clear API key.",
      });
    } finally {
      setSavingSecrets(false);
    }
  }

  useEffect(() => {
    if (!activeWorkspace) return;
    setName(activeWorkspace.name);
    setMyHandle(activeWorkspace.my_x_handle ?? "");
    setXProvider(activeWorkspace.x_data_provider);
    setLlmProvider(activeWorkspace.llm_provider);
    setPollMinutes(activeWorkspace.radar_poll_minutes);
    setUseXLists(activeWorkspace.use_x_lists);
    setXListIdsText((activeWorkspace.x_list_ids_json ?? []).join(", "));
    setMonthlyUnitBudget(activeWorkspace.monthly_unit_budget ?? 100_000);
    setHotPostsMinViews(activeWorkspace.hot_posts_min_projected_views ?? 5_000);
    setDailyCap(activeWorkspace.daily_spend_cap_usd);
    setMonthlyCap(activeWorkspace.monthly_spend_cap_usd);
    setWatchThreshold(activeWorkspace.watch_threshold);
    setSurgeThreshold(activeWorkspace.surge_threshold);
    setQuietStart(activeWorkspace.quiet_hours_start ?? "");
    setQuietEnd(activeWorkspace.quiet_hours_end ?? "");
    setMaxAlertsHour(activeWorkspace.max_alerts_per_hour);
    setWebhookUrls(activeWorkspace.webhook_urls_json ?? []);
  }, [activeWorkspace]);

  const loadAccuracySummary = useCallback(async () => {
    if (!activeWorkspace) return;
    try {
      const [sc, cl] = await Promise.all([
        backtestApi.getScorecard(activeWorkspace.id),
        backtestApi.getChangelog(activeWorkspace.id),
      ]);
      setScorecard(sc);
      setChangelog(cl);
    } catch {
      // Non-fatal in settings
    }
  }, [activeWorkspace]);

  useEffect(() => {
    void loadAccuracySummary();
  }, [loadAccuracySummary]);

  async function handleRestorePreviousWeights() {
    if (!activeWorkspace || !changelog || revertingWeights) return;
    const sorted = [...changelog.versions].sort(
      (a, b) => b.version_number - a.version_number
    );
    const activeIdx = sorted.findIndex((v) => v.is_effective_active);
    const previousVersion =
      activeIdx >= 0 && sorted[activeIdx + 1]
        ? sorted[activeIdx + 1]
        : sorted[sorted.length - 1];
    if (!previousVersion) return;

    setRevertingWeights(true);
    setBanner(null);
    try {
      await backtestApi.activateVersion(
        activeWorkspace.id,
        previousVersion.id,
        "Restored previous scoring weights from Settings"
      );
      await loadAccuracySummary();
      setBanner({
        kind: "ok",
        text: `Restored previous scoring weights (${previousVersion.label}).`,
      });
    } catch (err) {
      setBanner({
        kind: "err",
        text: err instanceof Error ? err.message : "Could not restore weights.",
      });
    } finally {
      setRevertingWeights(false);
    }
  }

  async function handleSaveSettings(e: React.FormEvent) {
    e.preventDefault();
    if (!activeWorkspace) return;
    setSaving(true);
    setBanner(null);
    try {
      const parsedXListIds = xListIdsText
        .split(/[\s,]+/)
        .map((s) => s.trim())
        .filter(Boolean);
      await updateWorkspace({
        name: name.trim(),
        my_x_handle: myHandle.trim() ? myHandle.trim() : null,
        x_data_provider: xProvider,
        llm_provider: llmProvider,
        radar_poll_minutes: Number(pollMinutes),
        use_x_lists: useXLists,
        x_list_ids_json: parsedXListIds,
        monthly_unit_budget: Number(monthlyUnitBudget),
        hot_posts_min_projected_views: Number(hotPostsMinViews),
        daily_spend_cap_usd: Number(dailyCap),
        monthly_spend_cap_usd: Number(monthlyCap),
        watch_threshold: Number(watchThreshold),
        surge_threshold: Number(surgeThreshold),
        quiet_hours_start: quietStart.trim() || null,
        quiet_hours_end: quietEnd.trim() || null,
        max_alerts_per_hour: Number(maxAlertsHour),
        webhook_urls_json: webhookUrls,
      });
      setBanner({
        kind: "ok",
        text: "Settings saved.",
      });
    } catch (err) {
      setBanner({
        kind: "err",
        text: err instanceof Error ? err.message : "Failed to save settings.",
      });
    } finally {
      setSaving(false);
    }
  }

  function handleAddWebhook() {
    const trimmed = newWebhookUrl.trim();
    if (!trimmed) return;
    if (!webhookUrls.includes(trimmed)) {
      setWebhookUrls((prev) => [...prev, trimmed]);
    }
    setNewWebhookUrl("");
  }

  async function handleReconcile() {
    setReconciling(true);
    setBanner(null);
    try {
      await reconcileSpend();
      setBanner({
        kind: "ok",
        text: "Synced provider usage.",
      });
    } catch (err) {
      setBanner({
        kind: "err",
        text: err instanceof Error ? err.message : "Usage sync failed.",
      });
    } finally {
      setReconciling(false);
    }
  }

  const hasTypedSecrets = Object.values(secretInputs).some((val) => val.trim().length > 0);

  const apiKeysSection = (
    <section className="rounded-2xl border border-slate-800 bg-surface-900/85 p-5 space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <Lock className="h-4 w-4 text-sky-400" />
          <div>
            <div className="text-sm font-semibold text-white">
              API Keys (Encrypted Vault)
            </div>
            <div className="text-[11px] text-slate-400">
              Stored encrypted at rest (Fernet AES-128-CBC + HMAC-SHA256). Server environment variables work automatically as fallbacks.
            </div>
          </div>
        </div>
      </div>

      <form onSubmit={handleSaveSecrets} className="space-y-3">
        <div className="grid grid-cols-1 gap-3">
          {secretsList.map((item) => {
            const draft = secretInputs[item.key_name] ?? "";
            return (
              <div
                key={item.key_name}
                className="rounded-xl bg-surface-950/75 border border-slate-800 p-3.5 space-y-2"
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div>
                    <div className="text-xs font-semibold text-slate-100 flex items-center gap-2">
                      <span>{item.label}</span>
                      <code className="text-[10px] font-mono text-slate-400 bg-slate-900 px-1.5 py-0.5 rounded">
                        {item.key_name}
                      </code>
                    </div>
                    <div className="text-[11px] text-slate-400 mt-0.5">
                      {item.description}
                    </div>
                  </div>

                  <div className="flex items-center gap-2">
                    {item.configured ? (
                      <span className="inline-flex items-center gap-1.5 rounded-full bg-emerald-500/15 border border-emerald-500/30 px-2.5 py-0.5 text-[11px] font-medium text-emerald-300">
                        <span className="h-1.5 w-1.5 rounded-full bg-emerald-400" />
                        {item.source === "app"
                          ? `Saved in App (${item.masked_hint})`
                          : `Server Env (${item.masked_hint})`}
                      </span>
                    ) : (
                      <span className="inline-flex items-center rounded-full bg-slate-800 border border-slate-700 px-2.5 py-0.5 text-[11px] text-slate-400">
                        Not set
                      </span>
                    )}

                    {item.source === "app" && (
                      <button
                        type="button"
                        disabled={savingSecrets}
                        onClick={() => void handleClearSecret(item.key_name)}
                        className="px-2 py-1 rounded-lg border border-slate-700 hover:border-rose-500/40 hover:text-rose-300 text-[11px] text-slate-300 transition-colors"
                        title={
                          item.has_env_fallback
                            ? "Revert to server environment variable"
                            : "Remove stored key"
                        }
                      >
                        {item.has_env_fallback ? "Use Env" : "Clear"}
                      </button>
                    )}
                  </div>
                </div>

                <input
                  type="password"
                  autoComplete="off"
                  value={draft}
                  onChange={(e) =>
                    setSecretInputs((prev) => ({
                      ...prev,
                      [item.key_name]: e.target.value,
                    }))
                  }
                  placeholder={
                    item.configured
                      ? `Replace active key (${item.masked_hint})...`
                      : `Paste ${item.key_name}...`
                  }
                  className="w-full rounded-lg bg-surface-850 border border-slate-700 px-3 py-2 text-xs text-white font-mono focus:outline-none focus:border-sky-500"
                />
              </div>
            );
          })}
        </div>

        <div className="flex justify-end pt-1">
          <button
            type="submit"
            disabled={savingSecrets || !hasTypedSecrets}
            className="inline-flex items-center gap-1.5 px-4 py-2 rounded-xl bg-sky-500 hover:bg-sky-400 text-slate-950 text-xs font-semibold transition-colors disabled:opacity-40"
          >
            <KeyRound className="h-3.5 w-3.5" />
            <span>{savingSecrets ? "Encrypting..." : "Save API Keys"}</span>
          </button>
        </div>
      </form>
    </section>
  );

  if (loading) {
    return (
      <div className="rounded-2xl border border-slate-800 bg-surface-900 p-8 text-center text-slate-400">
        Loading settings...
      </div>
    );
  }

  if (!activeWorkspace) {
    return (
      <div className="max-w-4xl mx-auto space-y-6">
        <div>
          <h1 className="text-xl font-semibold text-white tracking-tight">Settings</h1>
          <p className="text-xs text-slate-400 mt-0.5">
            Save your API keys below, then create your first workspace.
          </p>
        </div>

        {banner && (
          <div
            className={`rounded-xl border px-4 py-3 text-xs flex items-center gap-2.5 ${
              banner.kind === "ok"
                ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-200"
                : "border-rose-500/40 bg-rose-500/10 text-rose-200"
            }`}
          >
            <CheckCircle2 className="h-4 w-4 shrink-0" />
            <span>{banner.text}</span>
          </div>
        )}

        {apiKeysSection}

        <div className="rounded-2xl border border-slate-800 bg-surface-900 p-6 flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4">
          <div>
            <div className="text-sm font-semibold text-white">
              Ready to create your workspace?
            </div>
            <div className="text-xs text-slate-400 mt-0.5">
              Set your handle and import your seed accounts or X Lists.
            </div>
          </div>
          <button
            type="button"
            onClick={() => setShowNewWorkspaceModal(true)}
            className="inline-flex items-center gap-1.5 px-4 py-2 rounded-xl bg-sky-500 hover:bg-sky-400 text-slate-950 text-xs font-semibold shrink-0"
          >
            <Plus className="h-3.5 w-3.5" />
            <span>Create Workspace</span>
          </button>
        </div>
      </div>
    );
  }

  const credStatus = spendSummary?.provider_credentials_status ?? {};
  const dailySpent = spendSummary?.daily_spent_usd ?? 0;
  const monthlySpent = spendSummary?.monthly_spent_usd ?? 0;
  const dailyPct = Math.min(100, dailyCap > 0 ? (dailySpent / dailyCap) * 100 : 0);
  const monthlyPct = Math.min(
    100,
    monthlyCap > 0 ? (monthlySpent / monthlyCap) * 100 : 0
  );
  const unitsMonth = spendSummary?.units_month ?? 0;
  const projectedUnits = spendSummary?.projected_monthly_units ?? unitsMonth;
  const projectedUnitsPct = Math.min(
    100,
    monthlyUnitBudget > 0 ? (projectedUnits / monthlyUnitBudget) * 100 : 0
  );
  const throttleMode = spendSummary?.discovery_throttle_mode ?? "normal";

  const canUndoWeights = Boolean(
    changelog &&
      changelog.versions.length > 1 &&
      (scorecard?.active_version?.version_number ?? 1) > 1
  );

  return (
    <div className="max-w-4xl mx-auto space-y-6">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold text-white tracking-tight">
            Settings
          </h1>
          <p className="text-xs text-slate-400 mt-0.5">
            Automatic model tuning, data providers, X Lists, and alert rules.
          </p>
        </div>
        <button
          type="submit"
          form="workspace-settings-form"
          disabled={saving}
          className="inline-flex items-center gap-2 px-4 py-2 rounded-xl bg-sky-500 hover:bg-sky-400 text-slate-950 text-xs font-semibold transition-colors disabled:opacity-50 self-start"
        >
          <Save className="h-3.5 w-3.5" />
          <span>{saving ? "Saving..." : "Save Changes"}</span>
        </button>
      </div>

      {banner && (
        <div
          className={`rounded-xl border px-4 py-3 text-xs flex items-center gap-2.5 ${
            banner.kind === "ok"
              ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-200"
              : "border-rose-500/40 bg-rose-500/10 text-rose-200"
          }`}
        >
          <CheckCircle2 className="h-4 w-4 shrink-0" />
          <span>{banner.text}</span>
        </div>
      )}

      {/* 1. Zero-Touch Accuracy & Auto-Tuning Summary */}
      <section className="rounded-2xl border border-slate-800 bg-surface-900/85 p-5 space-y-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <Sparkles className="h-4 w-4 text-emerald-400" />
            <div>
              <div className="text-sm font-semibold text-white">
                Model Accuracy &amp; Automatic Tuning
              </div>
              <div className="text-[11px] text-slate-400">
                TweetTracker grades 48-hour post outcomes in the background and refines scoring weights automatically.
              </div>
            </div>
          </div>

          <div className="flex items-center gap-2">
            <span className="inline-flex items-center gap-1.5 rounded-full bg-emerald-500/15 border border-emerald-500/30 px-2.5 py-1 text-[11px] font-medium text-emerald-300">
              <span className="h-1.5 w-1.5 rounded-full bg-emerald-400" />
              Auto-Tuning Active
            </span>

            {canUndoWeights && (
              <button
                type="button"
                disabled={revertingWeights}
                onClick={() => void handleRestorePreviousWeights()}
                className="inline-flex items-center gap-1.5 rounded-lg border border-slate-700 bg-surface-850 hover:bg-surface-800 px-2.5 py-1 text-xs font-medium text-slate-200"
              >
                <RotateCcw className="h-3.5 w-3.5" />
                <span>{revertingWeights ? "Restoring..." : "Undo Last Tune"}</span>
              </button>
            )}
          </div>
        </div>

        {scorecard && (
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
            <div className="rounded-xl bg-surface-950/80 border border-slate-800 p-3.5">
              <div className="text-[11px] text-slate-400">Alert Hit Rate</div>
              <div className="text-xl font-bold text-white mt-0.5">
                {Math.round(scorecard.overall.precision * 100)}%
              </div>
              <div className="text-[11px] text-slate-500 mt-0.5">
                Flagged posts that became true 48h breakouts
              </div>
            </div>

            <div className="rounded-xl bg-surface-950/80 border border-slate-800 p-3.5">
              <div className="text-[11px] text-slate-400">Breakouts Caught</div>
              <div className="text-xl font-bold text-sky-300 mt-0.5">
                {Math.round(scorecard.overall.recall * 100)}%
              </div>
              <div className="text-[11px] text-slate-500 mt-0.5">
                Across {scorecard.overall.matured_posts_count} graded 48h posts
              </div>
            </div>

            <div className="rounded-xl bg-surface-950/80 border border-slate-800 p-3.5">
              <div className="text-[11px] text-slate-400">Early Head Start</div>
              <div className="text-xl font-bold text-emerald-300 mt-0.5">
                {Math.round(scorecard.overall.median_lead_time_growth_share * 100)}%
              </div>
              <div className="text-[11px] text-slate-500 mt-0.5">
                Views still ahead after post enters Hot Posts
              </div>
            </div>
          </div>
        )}
      </section>

      {/* 1b. Encrypted In-App API Keys */}
      {apiKeysSection}

      <form
        id="workspace-settings-form"
        onSubmit={handleSaveSettings}
        className="space-y-6"
      >
        {/* 2. Identity */}
        <section className="rounded-2xl border border-slate-800 bg-surface-900/80 p-5 space-y-4">
          <div className="flex items-center gap-2 text-sm font-semibold text-white">
            <Layers className="h-4 w-4 text-sky-400" />
            <span>Workspace Identity</span>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
              <label className="block text-xs font-medium text-slate-300 mb-1.5">
                Workspace Name
              </label>
              <input
                type="text"
                required
                value={name}
                onChange={(e) => setName(e.target.value)}
                className="w-full rounded-lg bg-surface-850 border border-slate-700 px-3.5 py-2 text-sm text-white focus:outline-none focus:border-sky-500"
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-300 mb-1.5">
                Your X Handle (Optional)
              </label>
              <input
                type="text"
                value={myHandle}
                onChange={(e) => setMyHandle(e.target.value)}
                placeholder="@your_handle"
                className="w-full rounded-lg bg-surface-850 border border-slate-700 px-3.5 py-2 text-sm text-white focus:outline-none focus:border-sky-500"
              />
              <p className="text-[11px] text-slate-400 mt-1">
                Used to highlight Mutuals and prioritize Reply vs. Quote actions.
              </p>
            </div>
          </div>
        </section>

        {/* 3. Providers & Polling */}
        <section className="rounded-2xl border border-slate-800 bg-surface-900/80 p-5 space-y-4">
          <div className="flex items-center gap-2 text-sm font-semibold text-white">
            <Database className="h-4 w-4 text-cyan-400" />
            <span>Data Source &amp; AI Model</span>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
              <label className="block text-xs font-medium text-slate-300 mb-1.5">
                X Data Source
              </label>
              <select
                value={xProvider}
                onChange={(e) => setXProvider(e.target.value as XDataProviderName)}
                className="w-full rounded-lg bg-surface-850 border border-slate-700 px-3.5 py-2 text-sm text-white"
              >
                <option value="fixture">Demo Data ($0 Offline)</option>
                <option value="tweetapi_com">TweetAPI.com Live</option>
                <option value="socialdata">SocialData.tools</option>
                <option value="twitterapi_io">TwitterAPI.io</option>
                <option value="official_x">Official X API v2</option>
              </select>
              <div className="text-[11px] mt-1.5 text-slate-400">
                API key:{" "}
                <span
                  className={
                    credStatus[xProvider] ? "text-emerald-400" : "text-amber-400"
                  }
                >
                  {credStatus[xProvider]
                    ? "Ready"
                    : "Not set — add key in API Keys above"}
                </span>
              </div>
            </div>

            <div>
              <label className="block text-xs font-medium text-slate-300 mb-1.5">
                AI Model (Algorithm Change Summaries)
              </label>
              <select
                value={llmProvider}
                onChange={(e) => setLlmProvider(e.target.value as LLMProviderName)}
                className="w-full rounded-lg bg-surface-850 border border-slate-700 px-3.5 py-2 text-sm text-white"
              >
                <option value="google_gemini">
                  Google Gemini (Gemini 3.8 Flash / 4 Argon)
                </option>
                <option value="xai_grok">xAI Grok (Grok 4.7)</option>
                <option value="openai">OpenAI</option>
                <option value="anthropic">Anthropic</option>
                <option value="deepinfra">DeepInfra</option>
              </select>
              <div className="text-[11px] mt-1.5 text-slate-400">
                API key:{" "}
                <span
                  className={
                    credStatus[llmProvider] ? "text-emerald-400" : "text-slate-300"
                  }
                >
                  {credStatus[llmProvider]
                    ? "Ready"
                    : "Using built-in offline summary fallback"}
                </span>
              </div>
            </div>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-4 pt-1">
            <div>
              <label className="block text-xs font-medium text-slate-300 mb-1.5">
                Refresh Interval (Minutes)
              </label>
              <input
                type="number"
                min={2}
                max={1440}
                value={pollMinutes}
                onChange={(e) => setPollMinutes(Number(e.target.value))}
                className="w-full rounded-lg bg-surface-850 border border-slate-700 px-3.5 py-2 text-sm text-white"
              />
            </div>

            <div className="space-y-2 pt-5">
              <label className="inline-flex items-center gap-3 cursor-pointer">
                <input
                  type="checkbox"
                  checked={useXLists}
                  onChange={(e) => setUseXLists(e.target.checked)}
                  className="h-4 w-4 rounded border-slate-600 bg-surface-850 text-sky-500"
                />
                <div>
                  <div className="text-xs text-slate-200 font-medium">
                    Use X Lists when available
                  </div>
                  <div className="text-[11px] text-slate-400">
                    Polls workspace X Lists in single API calls to lower credit usage.
                  </div>
                </div>
              </label>

              {useXLists && (
                <div>
                  <input
                    type="text"
                    value={xListIdsText}
                    onChange={(e) => setXListIdsText(e.target.value)}
                    placeholder="Optional X List IDs or URLs (comma-separated)"
                    className="w-full rounded-lg bg-surface-850 border border-slate-700 px-3 py-1.5 text-xs text-white font-mono focus:outline-none focus:border-sky-500"
                  />
                </div>
              )}
            </div>
          </div>
        </section>

        {/* 4. Spend Caps */}
        <section className="rounded-2xl border border-slate-800 bg-surface-900/80 p-5 space-y-4">
          <div className="flex items-center justify-between gap-4">
            <div className="flex items-center gap-2 text-sm font-semibold text-white">
              <Wallet className="h-4 w-4 text-emerald-400" />
              <span>Spend Guardrails</span>
            </div>
            <button
              type="button"
              onClick={() => void handleReconcile()}
              disabled={reconciling}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-slate-700 bg-surface-850 hover:bg-surface-800 text-xs text-slate-200"
            >
              <RefreshCw
                className={`h-3.5 w-3.5 ${reconciling ? "animate-spin" : ""}`}
              />
              <span>Sync Balance</span>
            </button>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
              <label className="block text-xs font-medium text-slate-300 mb-1.5">
                Daily Spend Limit (USD)
              </label>
              <input
                type="number"
                step="0.1"
                min={0}
                value={dailyCap}
                onChange={(e) => setDailyCap(Number(e.target.value))}
                className="w-full rounded-lg bg-surface-850 border border-slate-700 px-3.5 py-2 text-sm text-white"
              />
              <div className="mt-2 space-y-1">
                <div className="flex justify-between text-xs text-slate-400">
                  <span>Today: ${dailySpent.toFixed(2)}</span>
                  <span>{dailyPct.toFixed(0)}%</span>
                </div>
                <div className="h-1.5 rounded-full bg-surface-800 overflow-hidden">
                  <div
                    className="h-full bg-emerald-400 transition-all"
                    style={{ width: `${dailyPct}%` }}
                  />
                </div>
              </div>
            </div>

            <div>
              <label className="block text-xs font-medium text-slate-300 mb-1.5">
                Monthly Spend Limit (USD)
              </label>
              <input
                type="number"
                step="0.5"
                min={0}
                value={monthlyCap}
                onChange={(e) => setMonthlyCap(Number(e.target.value))}
                className="w-full rounded-lg bg-surface-850 border border-slate-700 px-3.5 py-2 text-sm text-white"
              />
              <div className="mt-2 space-y-1">
                <div className="flex justify-between text-xs text-slate-400">
                  <span>This Month: ${monthlySpent.toFixed(2)}</span>
                  <span>{monthlyPct.toFixed(0)}%</span>
                </div>
                <div className="h-1.5 rounded-full bg-surface-800 overflow-hidden">
                  <div
                    className="h-full bg-sky-400 transition-all"
                    style={{ width: `${monthlyPct}%` }}
                  />
                </div>
              </div>
            </div>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-4 pt-2 border-t border-slate-800/80">
            <div>
              <label className="block text-xs font-medium text-slate-300 mb-1.5">
                Monthly Unit Budget (TweetAPI Units)
              </label>
              <input
                type="number"
                step={1000}
                min={1000}
                max={10000000}
                value={monthlyUnitBudget}
                onChange={(e) => setMonthlyUnitBudget(Number(e.target.value))}
                className="w-full rounded-lg bg-surface-850 border border-slate-700 px-3.5 py-2 text-sm text-white"
              />
              <div className="mt-2 space-y-1">
                <div className="flex items-center justify-between text-xs text-slate-400">
                  <span>
                    Used: {unitsMonth.toLocaleString()} · Projected 30d:{" "}
                    {Math.round(projectedUnits).toLocaleString()}
                  </span>
                  <span
                    className={
                      throttleMode === "paused"
                        ? "text-rose-300 font-semibold"
                        : throttleMode === "throttled"
                          ? "text-amber-300 font-semibold"
                          : "text-emerald-300"
                    }
                  >
                    {throttleMode === "paused"
                      ? `Paused (${projectedUnitsPct.toFixed(0)}%)`
                      : throttleMode === "throttled"
                        ? `Throttled (${projectedUnitsPct.toFixed(0)}%)`
                        : `${projectedUnitsPct.toFixed(0)}%`}
                  </span>
                </div>
                <div className="h-1.5 rounded-full bg-surface-800 overflow-hidden">
                  <div
                    className={`h-full transition-all ${
                      throttleMode === "paused"
                        ? "bg-rose-400"
                        : throttleMode === "throttled"
                          ? "bg-amber-400"
                          : "bg-emerald-400"
                    }`}
                    style={{ width: `${projectedUnitsPct}%` }}
                  />
                </div>
              </div>
            </div>
          </div>

          <details className="text-xs text-slate-400 pt-1">
            <summary className="cursor-pointer hover:text-slate-200 select-none font-medium">
              Recent API usage log
            </summary>
            <div className="mt-2 border border-slate-800 rounded-xl overflow-hidden">
              <table className="w-full text-left text-xs">
                <thead className="bg-surface-850 text-slate-400 border-b border-slate-800">
                  <tr>
                    <th className="px-3 py-2 font-medium">Provider</th>
                    <th className="px-3 py-2 font-medium">Endpoint</th>
                    <th className="px-3 py-2 font-medium text-right">Units</th>
                    <th className="px-3 py-2 font-medium text-right">USD</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-800/70">
                  {(spendSummary?.recent_ledger ?? []).slice(0, 6).map((entry) => (
                    <tr key={entry.id} className="text-slate-300">
                      <td className="px-3 py-1.5">{entry.provider}</td>
                      <td className="px-3 py-1.5 text-slate-400">{entry.endpoint}</td>
                      <td className="px-3 py-1.5 text-right">{entry.units_consumed}</td>
                      <td className="px-3 py-1.5 text-right">
                        ${entry.estimated_usd.toFixed(4)}
                      </td>
                    </tr>
                  ))}
                  {!spendSummary?.recent_ledger?.length && (
                    <tr>
                      <td colSpan={4} className="px-3 py-3 text-center text-slate-500">
                        No paid API calls logged yet.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </details>
        </section>

        {/* 5. Alerts & Webhooks */}
        <section className="rounded-2xl border border-slate-800 bg-surface-900/80 p-5 space-y-4">
          <div className="flex items-center gap-2 text-sm font-semibold text-white">
            <Bell className="h-4 w-4 text-violet-400" />
            <span>Alert Rules &amp; Webhooks</span>
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
            <div>
              <label className="block text-xs font-medium text-slate-300 mb-1">
                Heating Up Score (0–100)
              </label>
              <input
                type="number"
                step="0.5"
                value={watchThreshold}
                onChange={(e) => setWatchThreshold(Number(e.target.value))}
                className="w-full rounded-lg bg-surface-850 border border-slate-700 px-3 py-2 text-sm text-white"
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-300 mb-1">
                Surging Score (0–100)
              </label>
              <input
                type="number"
                step="0.5"
                value={surgeThreshold}
                onChange={(e) => setSurgeThreshold(Number(e.target.value))}
                className="w-full rounded-lg bg-surface-850 border border-slate-700 px-3 py-2 text-sm text-white"
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-300 mb-1">
                Max Alerts / Hour
              </label>
              <input
                type="number"
                min={1}
                value={maxAlertsHour}
                onChange={(e) => setMaxAlertsHour(Number(e.target.value))}
                className="w-full rounded-lg bg-surface-850 border border-slate-700 px-3 py-2 text-sm text-white"
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-300 mb-1">
                Hot Posts Min. Projected Views
              </label>
              <input
                type="number"
                step={500}
                min={0}
                value={hotPostsMinViews}
                onChange={(e) => setHotPostsMinViews(Number(e.target.value))}
                className="w-full rounded-lg bg-surface-850 border border-slate-700 px-3 py-2 text-sm text-white"
              />
              <p className="mt-1 text-[11px] text-slate-500">
                Hides posts projected to stay under this many views by 48h. 0 shows everything.
              </p>
            </div>
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div>
              <label className="block text-xs font-medium text-slate-300 mb-1">
                Quiet Hours Start
              </label>
              <input
                type="time"
                value={quietStart}
                onChange={(e) => setQuietStart(e.target.value)}
                className="w-full rounded-lg bg-surface-850 border border-slate-700 px-3 py-2 text-sm text-white"
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-300 mb-1">
                Quiet Hours End
              </label>
              <input
                type="time"
                value={quietEnd}
                onChange={(e) => setQuietEnd(e.target.value)}
                className="w-full rounded-lg bg-surface-850 border border-slate-700 px-3 py-2 text-sm text-white"
              />
            </div>
          </div>

          <div className="pt-1">
            <label className="block text-xs font-medium text-slate-300 mb-1.5 flex items-center gap-1.5">
              <Webhook className="h-3.5 w-3.5 text-sky-400" />
              <span>Webhook URLs (Slack, Zapier, n8n)</span>
            </label>
            <div className="flex gap-2">
              <input
                type="url"
                value={newWebhookUrl}
                onChange={(e) => setNewWebhookUrl(e.target.value)}
                placeholder="https://hooks.slack.com/..."
                className="flex-1 rounded-lg bg-surface-850 border border-slate-700 px-3.5 py-2 text-sm text-white"
              />
              <button
                type="button"
                onClick={handleAddWebhook}
                className="px-3.5 py-2 rounded-lg bg-surface-800 hover:bg-slate-700 border border-slate-600 text-xs font-medium text-slate-100"
              >
                Add
              </button>
            </div>
            {webhookUrls.length > 0 && (
              <div className="mt-2.5 space-y-1.5">
                {webhookUrls.map((url) => (
                  <div
                    key={url}
                    className="flex items-center justify-between px-3 py-1.5 rounded-lg bg-surface-850 border border-slate-800 text-xs text-slate-300"
                  >
                    <span className="truncate">{url}</span>
                    <button
                      type="button"
                      onClick={() =>
                        setWebhookUrls((prev) => prev.filter((item) => item !== url))
                      }
                      className="text-slate-400 hover:text-rose-400 ml-3"
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </button>
                  </div>
                ))}
              </div>
            )}
          </div>
        </section>
      </form>

      {/* 6. Advanced Diagnostics & Upstream Algorithm Sync (Collapsed by Default) */}
      <details className="rounded-2xl border border-slate-800 bg-surface-900/60 p-5 group">
        <summary className="cursor-pointer select-none text-sm font-semibold text-slate-200 flex items-center justify-between">
          <span>Webhook Delivery Tester &amp; Upstream X Algorithm Sync</span>
          <span className="text-xs font-normal text-slate-400">Click to expand</span>
        </summary>
        <div className="mt-5 space-y-6">
          <WebhookDeliveryPanel workspaceId={activeWorkspace.id} />
          <AlgoSyncPanel />
        </div>
      </details>

      {/* 8. Session & Delete Workspace */}
      <section className="rounded-2xl border border-slate-800 bg-surface-900/80 p-5 flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4">
        <div className="space-y-0.5">
          <div className="flex items-center gap-2 text-sm font-semibold text-white">
            <KeyRound className="h-4 w-4 text-slate-400" />
            <span>Workspace Access</span>
          </div>
          <p className="text-xs text-slate-400">
            Manage password session or delete this workspace.
          </p>
        </div>

        <div className="flex items-center gap-2.5">
          {authRequired && isAuthenticated && (
            <button
              type="button"
              onClick={clearAdminSession}
              className="px-3 py-1.5 rounded-lg border border-slate-700 text-xs text-slate-300 hover:bg-surface-800"
            >
              Lock Session
            </button>
          )}
          <button
            type="button"
            onClick={() => {
              if (
                typeof window === "undefined" ||
                window.confirm(`Delete workspace "${activeWorkspace.name}"?`)
              ) {
                void deleteWorkspace(activeWorkspace.id);
              }
            }}
            className="px-3 py-1.5 rounded-lg border border-rose-500/40 bg-rose-500/10 hover:bg-rose-500/20 text-xs font-medium text-rose-300"
          >
            Delete Workspace
          </button>
        </div>
      </section>
    </div>
  );
}
