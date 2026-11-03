"use client";

import React, { useEffect, useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  Activity,
  CheckCircle2,
  ChevronDown,
  Flame,
  KeyRound,
  Lock,
  Network,
  Plus,
  Settings,
  X,
} from "lucide-react";
import { useWorkspace } from "./workspace-context";
import { SSEAlertCenter } from "./radar/sse-alert-center";
import type { LLMProviderName, XDataProviderName } from "@/lib/types";

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const {
    workspaces,
    activeWorkspace,
    health,
    loading,
    error,
    authRequired,
    isAuthenticated,
    showAuthDialog,
    setShowAuthDialog,
    showNewWorkspaceModal,
    setShowNewWorkspaceModal,
    selectWorkspace,
    refreshAll,
    createWorkspace,
    loginWithPassword,
  } = useWorkspace();

  const [wsDropdownOpen, setWsDropdownOpen] = useState(false);

  // New Workspace Modal form state
  const [newWsName, setNewWsName] = useState("");
  const [newWsHandle, setNewWsHandle] = useState("");
  const [newWsXProvider, setNewWsXProvider] = useState<XDataProviderName>("tweetapi_com");
  const [newWsLlmProvider, setNewWsLlmProvider] = useState<LLMProviderName>("google_gemini");

  useEffect(() => {
    if (health?.providers_configured) {
      setNewWsXProvider(
        health.providers_configured.tweetapi_com ? "tweetapi_com" : "fixture"
      );
    }
  }, [health?.providers_configured]);
  const [creatingWs, setCreatingWs] = useState(false);
  const [modalError, setModalError] = useState<string | null>(null);

  // Admin Password Dialog state
  const [pwInput, setPwInput] = useState("");
  const [pwError, setPwError] = useState<string | null>(null);
  const [verifyingPw, setVerifyingPw] = useState(false);

  const isHotPosts = pathname === "/" || pathname.startsWith("/radar");
  const isNetwork = pathname.startsWith("/network") || pathname.startsWith("/rising");
  const isSettings = pathname.startsWith("/settings") || pathname.startsWith("/backtest");

  async function handleCreateWorkspace(e: React.FormEvent) {
    e.preventDefault();
    if (!newWsName.trim()) return;
    setCreatingWs(true);
    setModalError(null);
    try {
      await createWorkspace({
        name: newWsName.trim(),
        my_x_handle: newWsHandle.trim() || null,
        x_data_provider: newWsXProvider,
        llm_provider: newWsLlmProvider,
      });
      setNewWsName("");
      setNewWsHandle("");
      setShowNewWorkspaceModal(false);
      setWsDropdownOpen(false);
    } catch (err) {
      setModalError(err instanceof Error ? err.message : "Failed to create workspace.");
    } finally {
      setCreatingWs(false);
    }
  }

  async function handleLoginSubmit(e: React.FormEvent) {
    e.preventDefault();
    setVerifyingPw(true);
    setPwError(null);
    try {
      await loginWithPassword(pwInput);
      setPwInput("");
    } catch (err) {
      setPwError(err instanceof Error ? err.message : "Invalid password.");
    } finally {
      setVerifyingPw(false);
    }
  }

  // Non-dismissible full-screen Password Gate when TWEETTRACKER_ADMIN_PASSWORD is active
  if (authRequired && !isAuthenticated) {
    return (
      <div className="min-h-screen bg-surface-950 text-slate-100 flex items-center justify-center p-4">
        <div className="w-full max-w-md rounded-2xl bg-surface-900 border border-slate-800 shadow-2xl p-6 space-y-5">
          <div className="flex items-center gap-3">
            <div className="h-10 w-10 rounded-xl bg-gradient-to-br from-sky-500/20 to-emerald-500/20 border border-sky-400/30 flex items-center justify-center">
              <Lock className="h-5 w-5 text-sky-400" />
            </div>
            <div>
              <h1 className="text-base font-semibold text-white tracking-tight">
                TweetTracker Locked
              </h1>
              <p className="text-xs text-slate-400">
                Enter your password to unlock your workspaces and live radar.
              </p>
            </div>
          </div>

          <form onSubmit={handleLoginSubmit} className="space-y-4">
            <input
              type="password"
              required
              autoFocus
              value={pwInput}
              onChange={(e) => setPwInput(e.target.value)}
              placeholder="Workspace password"
              className="w-full rounded-xl bg-surface-850 border border-slate-700 px-3.5 py-2.5 text-sm text-white focus:outline-none focus:border-sky-500"
            />
            {pwError && (
              <div className="text-xs text-rose-400 bg-rose-500/10 border border-rose-500/30 rounded-lg px-3 py-2">
                {pwError}
              </div>
            )}
            <button
              type="submit"
              disabled={verifyingPw}
              className="w-full py-2.5 rounded-xl bg-sky-500 hover:bg-sky-400 text-slate-950 text-xs font-semibold transition-colors disabled:opacity-50"
            >
              {verifyingPw ? "Unlocking..." : "Unlock"}
            </button>
          </form>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-surface-950 text-slate-100 flex flex-col">
      {/* Clean Single Top Bar */}
      <header className="h-14 border-b border-slate-800/80 bg-surface-900/90 backdrop-blur sticky top-0 z-30">
        <div className="max-w-7xl mx-auto h-full px-4 sm:px-6 lg:px-8 flex items-center justify-between gap-3">
          {/* Left: Brand + Workspace Switcher */}
          <div className="flex items-center gap-3 min-w-0">
            <Link href="/radar" className="flex items-center gap-2 shrink-0 group">
              <div className="h-8 w-8 rounded-lg bg-gradient-to-br from-sky-500/20 to-emerald-500/20 border border-sky-400/30 flex items-center justify-center">
                <Activity className="h-4 w-4 text-sky-400 group-hover:scale-105 transition-transform" />
              </div>
              <span className="font-semibold tracking-tight text-slate-100 hidden sm:inline">
                TweetTracker
              </span>
            </Link>

            <span className="text-slate-700 hidden sm:inline">/</span>

            {/* Workspace Dropdown */}
            <div className="relative">
              <button
                type="button"
                onClick={() => setWsDropdownOpen((prev) => !prev)}
                className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-surface-850 hover:bg-surface-800 border border-slate-800 text-left transition-colors max-w-[240px]"
              >
                <span className="h-2 w-2 rounded-full bg-sky-400 shrink-0" />
                <span className="text-xs font-medium text-slate-100 truncate">
                  {activeWorkspace ? activeWorkspace.name : "Select Workspace"}
                </span>
                <ChevronDown className="h-3.5 w-3.5 text-slate-400 shrink-0" />
              </button>

              {wsDropdownOpen && (
                <>
                  <div
                    className="fixed inset-0 z-40"
                    onClick={() => setWsDropdownOpen(false)}
                  />
                  <div className="absolute left-0 mt-2 w-72 rounded-xl bg-surface-900 border border-slate-700 shadow-2xl py-2 z-50">
                    <div className="px-3.5 py-1.5 text-[11px] font-semibold uppercase tracking-wider text-slate-400">
                      Workspaces
                    </div>
                    <div className="max-h-72 overflow-y-auto divide-y divide-slate-800/70">
                      {workspaces.map((ws) => {
                        const isSelected = ws.id === activeWorkspace?.id;
                        return (
                          <button
                            key={ws.id}
                            type="button"
                            onClick={() => {
                              selectWorkspace(ws.id);
                              setWsDropdownOpen(false);
                            }}
                            className={`w-full px-3.5 py-2.5 text-left hover:bg-surface-800 transition-colors flex items-center justify-between gap-2 ${
                              isSelected ? "bg-sky-500/10" : ""
                            }`}
                          >
                            <div className="min-w-0">
                              <div className="text-sm font-medium text-slate-100 truncate">
                                {ws.name}
                              </div>
                              <div className="text-xs text-slate-400 mt-0.5 flex items-center gap-2">
                                {ws.my_x_handle && (
                                  <>
                                    <span className="text-sky-400">@{ws.my_x_handle}</span>
                                    <span>·</span>
                                  </>
                                )}
                                <span>{ws.tracked_accounts_count} people</span>
                              </div>
                            </div>
                            {isSelected && (
                              <span className="text-[10px] font-semibold px-1.5 py-0.5 rounded bg-sky-500/20 text-sky-300 shrink-0">
                                Active
                              </span>
                            )}
                          </button>
                        );
                      })}
                    </div>
                    <div className="border-t border-slate-800 mt-1 pt-2 px-2">
                      <button
                        type="button"
                        onClick={() => {
                          setWsDropdownOpen(false);
                          setShowNewWorkspaceModal(true);
                        }}
                        className="w-full flex items-center justify-center gap-1.5 px-3 py-2 rounded-lg bg-sky-500/15 hover:bg-sky-500/25 text-sky-300 border border-sky-500/30 text-xs font-medium transition-colors"
                      >
                        <Plus className="h-3.5 w-3.5" />
                        <span>New Workspace</span>
                      </button>
                    </div>
                  </div>
                </>
              )}
            </div>
          </div>

          {/* Center: 2 Primary Views Segmented Toggle */}
          <nav className="flex items-center rounded-xl bg-surface-950/90 border border-slate-800 p-1">
            <Link
              href="/radar"
              className={`inline-flex items-center gap-1.5 px-3.5 py-1.5 rounded-lg text-xs font-semibold transition-all ${
                isHotPosts
                  ? "bg-sky-500/20 text-sky-200 border border-sky-500/40 shadow-sm"
                  : "text-slate-400 hover:text-slate-100 border border-transparent"
              }`}
            >
              <Flame
                className={`h-3.5 w-3.5 ${
                  isHotPosts ? "text-amber-400" : "text-slate-400"
                }`}
              />
              <span>Hot Posts</span>
            </Link>
            <Link
              href="/network"
              className={`inline-flex items-center gap-1.5 px-3.5 py-1.5 rounded-lg text-xs font-semibold transition-all ${
                isNetwork
                  ? "bg-sky-500/20 text-sky-200 border border-sky-500/40 shadow-sm"
                  : "text-slate-400 hover:text-slate-100 border border-transparent"
              }`}
            >
              <Network
                className={`h-3.5 w-3.5 ${
                  isNetwork ? "text-sky-400" : "text-slate-400"
                }`}
              />
              <span>Network</span>
            </Link>
          </nav>

          {/* Right: Live Bell + Optional Lock + Settings Gear */}
          <div className="flex items-center gap-2 shrink-0">
            {activeWorkspace && !isHotPosts && (
              <SSEAlertCenter workspaceId={activeWorkspace.id} />
            )}

            {authRequired && (
              <button
                type="button"
                onClick={() => setShowAuthDialog(true)}
                className={`p-2 rounded-lg border text-xs transition-colors ${
                  isAuthenticated
                    ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-300"
                    : "border-rose-500/40 bg-rose-500/10 text-rose-300"
                }`}
                title="Workspace Lock"
              >
                <Lock className="h-4 w-4" />
              </button>
            )}

            <Link
              href="/settings"
              className={`p-2 rounded-lg border transition-colors ${
                isSettings
                  ? "border-sky-500/40 bg-sky-500/15 text-sky-300"
                  : "border-slate-800 bg-surface-850 hover:bg-surface-800 text-slate-400 hover:text-slate-100"
              }`}
              title="Settings & Auto-Tuning"
            >
              <Settings className="h-4 w-4" />
            </Link>
          </div>
        </div>
      </header>

      {/* Main Content */}
      <main className="flex-1 max-w-7xl w-full mx-auto px-4 sm:px-6 lg:px-8 py-6">
        {error && (
          <div className="mb-5 rounded-xl border border-amber-500/40 bg-amber-500/10 p-4 text-sm text-amber-200 flex items-center justify-between gap-4">
            <div>
              <span className="font-semibold">Connection notice: </span>
              <span>{error}</span>
            </div>
            <button
              type="button"
              onClick={() => void refreshAll()}
              className="px-3 py-1.5 rounded-lg bg-amber-500/20 hover:bg-amber-500/30 text-xs font-medium shrink-0"
            >
              Retry
            </button>
          </div>
        )}
        {!loading && workspaces.length === 0 && !isSettings ? (
          <div className="max-w-xl mx-auto mt-10 rounded-2xl border border-slate-800 bg-surface-900/90 p-7 space-y-5 shadow-xl">
            <div className="space-y-1.5">
              <div className="inline-flex items-center gap-1.5 rounded-full bg-sky-500/15 border border-sky-500/30 px-2.5 py-0.5 text-[11px] font-medium text-sky-300">
                Clean Server Ready
              </div>
              <h1 className="text-xl font-semibold text-white tracking-tight">
                Set Up Your First Workspace
              </h1>
              <p className="text-xs text-slate-400 leading-relaxed">
                Add your API key in Settings, create your workspace with your X handle, then paste seed accounts or an X List on the Network tab to crawl your live social graph.
              </p>
            </div>

            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 pt-1">
              <Link
                href="/settings"
                className="flex items-center justify-between gap-2 rounded-xl border border-slate-700 bg-surface-850 hover:bg-surface-800 px-4 py-3 text-xs font-medium text-slate-200 transition-colors"
              >
                <span className="inline-flex items-center gap-2">
                  <KeyRound className="h-4 w-4 text-sky-400" />
                  <span>1. Set API Keys</span>
                </span>
                {health?.providers_configured?.tweetapi_com && (
                  <CheckCircle2 className="h-4 w-4 text-emerald-400 shrink-0" />
                )}
              </Link>

              <button
                type="button"
                onClick={() => setShowNewWorkspaceModal(true)}
                className="flex items-center justify-center gap-2 rounded-xl bg-sky-500 hover:bg-sky-400 px-4 py-3 text-xs font-semibold text-slate-950 transition-colors"
              >
                <Plus className="h-4 w-4" />
                <span>2. Create Workspace</span>
              </button>
            </div>
          </div>
        ) : (
          children
        )}
      </main>

      {/* New Workspace Modal */}
      {showNewWorkspaceModal && (
        <div className="fixed inset-0 z-50 bg-black/70 backdrop-blur-sm flex items-center justify-center p-4">
          <div className="w-full max-w-lg rounded-2xl bg-surface-900 border border-slate-700 shadow-2xl p-6">
            <div className="flex items-center justify-between mb-4">
              <div>
                <h2 className="text-lg font-semibold text-white">New Workspace</h2>
                <p className="text-xs text-slate-400 mt-0.5">
                  Track your X network and surface early breakout posts to reply or quote.
                </p>
              </div>
              <button
                type="button"
                onClick={() => setShowNewWorkspaceModal(false)}
                className="p-1.5 rounded-lg hover:bg-surface-800 text-slate-400"
              >
                <X className="h-4 w-4" />
              </button>
            </div>

            <form onSubmit={handleCreateWorkspace} className="space-y-4">
              <div>
                <label className="block text-xs font-medium text-slate-300 mb-1">
                  Workspace Name *
                </label>
                <input
                  type="text"
                  required
                  value={newWsName}
                  onChange={(e) => setNewWsName(e.target.value)}
                  placeholder="e.g., GentOfTech"
                  className="w-full rounded-lg bg-surface-850 border border-slate-700 px-3 py-2 text-sm text-white focus:outline-none focus:border-sky-500"
                />
              </div>

              <div>
                <label className="block text-xs font-medium text-slate-300 mb-1">
                  Your X Handle (Optional)
                </label>
                <input
                  type="text"
                  value={newWsHandle}
                  onChange={(e) => setNewWsHandle(e.target.value)}
                  placeholder="@your_handle — enables Mutual reply recommendations"
                  className="w-full rounded-lg bg-surface-850 border border-slate-700 px-3 py-2 text-sm text-white focus:outline-none focus:border-sky-500"
                />
              </div>

              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-xs font-medium text-slate-300 mb-1">
                    X Data Source
                  </label>
                  <select
                    value={newWsXProvider}
                    onChange={(e) => setNewWsXProvider(e.target.value as XDataProviderName)}
                    className="w-full rounded-lg bg-surface-850 border border-slate-700 px-3 py-2 text-sm text-white"
                  >
                    <option value="fixture">Demo Data ($0 Offline)</option>
                    <option value="tweetapi_com">TweetAPI.com Live</option>
                    <option value="socialdata">SocialData</option>
                    <option value="twitterapi_io">TwitterAPI.io</option>
                    <option value="official_x">Official X API v2</option>
                  </select>
                </div>
                <div>
                  <label className="block text-xs font-medium text-slate-300 mb-1">
                    AI Model
                  </label>
                  <select
                    value={newWsLlmProvider}
                    onChange={(e) => setNewWsLlmProvider(e.target.value as LLMProviderName)}
                    className="w-full rounded-lg bg-surface-850 border border-slate-700 px-3 py-2 text-sm text-white"
                  >
                    <option value="google_gemini">Google Gemini (Gemini 3.8 Flash)</option>
                    <option value="xai_grok">xAI Grok (Grok 4.7)</option>
                    <option value="openai">OpenAI</option>
                    <option value="anthropic">Anthropic</option>
                    <option value="deepinfra">DeepInfra</option>
                  </select>
                </div>
              </div>

              {modalError && (
                <div className="text-xs text-rose-400 bg-rose-500/10 border border-rose-500/30 rounded-lg px-3 py-2">
                  {modalError}
                </div>
              )}

              <div className="flex justify-end gap-2 pt-2">
                <button
                  type="button"
                  onClick={() => setShowNewWorkspaceModal(false)}
                  className="px-4 py-2 rounded-lg border border-slate-700 text-xs font-medium text-slate-300 hover:bg-surface-800"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={creatingWs}
                  className="px-4 py-2 rounded-lg bg-sky-500 hover:bg-sky-400 text-slate-950 text-xs font-semibold disabled:opacity-50"
                >
                  {creatingWs ? "Creating..." : "Create Workspace"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Password Login Dialog */}
      {showAuthDialog && (
        <div className="fixed inset-0 z-50 bg-black/75 backdrop-blur-sm flex items-center justify-center p-4">
          <div className="w-full max-w-md rounded-2xl bg-surface-900 border border-slate-700 shadow-2xl p-6">
            <div className="flex items-center justify-between mb-3">
              <div className="flex items-center gap-2">
                <Lock className="h-4 w-4 text-sky-400" />
                <h2 className="text-base font-semibold text-white">Unlock Workspace</h2>
              </div>
              {(!authRequired || isAuthenticated) && (
                <button
                  type="button"
                  onClick={() => setShowAuthDialog(false)}
                  className="p-1 rounded-lg hover:bg-surface-800 text-slate-400"
                >
                  <X className="h-4 w-4" />
                </button>
              )}
            </div>

            <form onSubmit={handleLoginSubmit} className="space-y-4">
              <p className="text-xs text-slate-400">
                Enter your workspace password to unlock this browser session.
              </p>
              <input
                type="password"
                required
                value={pwInput}
                onChange={(e) => setPwInput(e.target.value)}
                placeholder="Password"
                className="w-full rounded-lg bg-surface-850 border border-slate-700 px-3 py-2 text-sm text-white focus:outline-none focus:border-sky-500"
              />
              {pwError && (
                <div className="text-xs text-rose-400 bg-rose-500/10 border border-rose-500/30 rounded-lg px-3 py-2">
                  {pwError}
                </div>
              )}
              <div className="flex justify-end gap-2">
                {isAuthenticated && (
                  <button
                    type="button"
                    onClick={() => setShowAuthDialog(false)}
                    className="px-4 py-2 rounded-lg border border-slate-700 text-xs text-slate-300"
                  >
                    Cancel
                  </button>
                )}
                <button
                  type="submit"
                  disabled={verifyingPw}
                  className="px-4 py-2 rounded-lg bg-sky-500 hover:bg-sky-400 text-slate-950 text-xs font-semibold"
                >
                  {verifyingPw ? "Verifying..." : "Unlock"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
