"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import {
  Activity,
  Bell,
  CheckCircle2,
  Flame,
  Volume2,
  VolumeX,
  Wifi,
  WifiOff,
  X,
} from "lucide-react";
import { radarApi } from "@/lib/radar-api";
import type { AlertDeliveryItem, RadarSSEEvent } from "@/lib/radar-types";

const CHIME_STORAGE_KEY = "tweettracker_radar_chime_enabled";

interface SSEAlertCenterProps {
  workspaceId: number;
  onRadarUpdate?: (event: RadarSSEEvent) => void;
  onSelectPostId?: (postId: number) => void;
}

interface ToastItem {
  id: string;
  tier: "surge" | "watch";
  authorHandle: string;
  score: number;
  textPreview: string;
  postId: number | null;
  createdAt: string;
}

function playAlertChime(tier: "surge" | "watch") {
  if (typeof window === "undefined") return;
  const AudioCtx =
    window.AudioContext ||
    (window as unknown as { webkitAudioContext?: typeof AudioContext })
      .webkitAudioContext;
  if (!AudioCtx) return;

  try {
    const ctx = new AudioCtx();
    const now = ctx.currentTime;
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();

    osc.type = "sine";
    if (tier === "surge") {
      osc.frequency.setValueAtTime(660, now);
      osc.frequency.exponentialRampToValueAtTime(988, now + 0.14);
      gain.gain.setValueAtTime(0.08, now);
      gain.gain.exponentialRampToValueAtTime(0.001, now + 0.34);
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.start(now);
      osc.stop(now + 0.35);
    } else {
      osc.frequency.setValueAtTime(523.25, now);
      osc.frequency.exponentialRampToValueAtTime(659.25, now + 0.12);
      gain.gain.setValueAtTime(0.05, now);
      gain.gain.exponentialRampToValueAtTime(0.001, now + 0.25);
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.start(now);
      osc.stop(now + 0.26);
    }
    setTimeout(() => {
      void ctx.close().catch(() => undefined);
    }, 500);
  } catch {
    // Browser autoplay policies can block AudioContext until user gesture
  }
}

export function SSEAlertCenter({
  workspaceId,
  onRadarUpdate,
  onSelectPostId,
}: SSEAlertCenterProps) {
  const [connectionState, setConnectionState] = useState<
    "connecting" | "live" | "reconnecting"
  >("connecting");
  const [chimeEnabled, setChimeEnabled] = useState<boolean>(false);
  const [drawerOpen, setDrawerOpen] = useState<boolean>(false);
  const [unreadCount, setUnreadCount] = useState<number>(0);
  const [liveEvents, setLiveEvents] = useState<RadarSSEEvent[]>([]);
  const [storedAlerts, setStoredAlerts] = useState<AlertDeliveryItem[]>([]);
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const [lastHeartbeat, setLastHeartbeat] = useState<string | null>(null);

  const chimeEnabledRef = useRef<boolean>(false);
  const onRadarUpdateRef = useRef<SSEAlertCenterProps["onRadarUpdate"]>(onRadarUpdate);

  useEffect(() => {
    onRadarUpdateRef.current = onRadarUpdate;
  }, [onRadarUpdate]);

  useEffect(() => {
    if (typeof window !== "undefined") {
      const saved = window.localStorage.getItem(CHIME_STORAGE_KEY) === "true";
      setChimeEnabled(saved);
      chimeEnabledRef.current = saved;
    }
  }, []);

  const toggleChime = useCallback(() => {
    setChimeEnabled((prev) => {
      const next = !prev;
      chimeEnabledRef.current = next;
      if (typeof window !== "undefined") {
        window.localStorage.setItem(CHIME_STORAGE_KEY, String(next));
      }
      if (next) {
        playAlertChime("watch");
      }
      return next;
    });
  }, []);

  const loadAlertHistory = useCallback(async () => {
    try {
      const res = await radarApi.listAlerts(workspaceId, { limit: 35 });
      setStoredAlerts(res.items);
    } catch {
      // Non-fatal in alert center
    }
  }, [workspaceId]);

  useEffect(() => {
    setLiveEvents([]);
    setUnreadCount(0);
    setToasts([]);
    void loadAlertHistory();
  }, [workspaceId, loadAlertHistory]);

  useEffect(() => {
    if (typeof window === "undefined" || typeof EventSource === "undefined") {
      return;
    }

    let cancelled = false;
    let source: EventSource | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

    const parsePayload = (raw: MessageEvent): RadarSSEEvent | null => {
      try {
        return JSON.parse(raw.data) as RadarSSEEvent;
      } catch {
        return null;
      }
    };

    const connect = async () => {
      if (cancelled) return;
      setConnectionState((prev) => (prev === "reconnecting" ? "reconnecting" : "connecting"));
      const url = await radarApi.resolveEventsSseUrl(workspaceId);
      if (cancelled) return;

      const es = new EventSource(url);
      source = es;

      es.onopen = () => {
        if (!cancelled) {
          setConnectionState("live");
        }
      };

      es.onerror = () => {
        if (cancelled) return;
        setConnectionState("reconnecting");
        es.close();
        if (reconnectTimer) clearTimeout(reconnectTimer);
        reconnectTimer = setTimeout(() => {
          void connect();
        }, 3000);
      };

      es.addEventListener("connected", (evt) => {
        setConnectionState("live");
        const parsed = parsePayload(evt as MessageEvent);
        if (parsed) {
          setLastHeartbeat(parsed.timestamp);
        }
      });

      es.addEventListener("heartbeat", (evt) => {
        setConnectionState("live");
        const parsed = parsePayload(evt as MessageEvent);
        if (parsed) {
          setLastHeartbeat(parsed.timestamp);
        }
      });

      const handleBreakoutAlert = (tier: "surge" | "watch") => (evt: Event) => {
        const parsed = parsePayload(evt as MessageEvent);
        if (!parsed) return;

        setLiveEvents((prev) => [parsed, ...prev].slice(0, 40));
        setUnreadCount((c) => c + 1);

        const data = parsed.data || {};
        const authorObj = (data.author as Record<string, unknown> | undefined) || {};
        const authorHandle = String(
          authorObj.handle || data.author_handle || "tracked_voice"
        );
        const score = Number(data.breakout_score ?? 0);
        const textPreview = String(
          data.text_preview || data.text || "Breakout velocity threshold crossed."
        );
        const postId =
          typeof data.post_id === "number"
            ? data.post_id
            : data.post_id
            ? Number(data.post_id)
            : null;

        const toast: ToastItem = {
          id: parsed.id || `${Date.now()}-${Math.random()}`,
          tier,
          authorHandle,
          score,
          textPreview,
          postId,
          createdAt: parsed.timestamp,
        };
        setToasts((prev) => [toast, ...prev].slice(0, 3));

        if (chimeEnabledRef.current) {
          playAlertChime(tier);
        }

        onRadarUpdateRef.current?.(parsed);
        void loadAlertHistory();
      };

      es.addEventListener("breakout.surge", handleBreakoutAlert("surge"));
      es.addEventListener("breakout.watch", handleBreakoutAlert("watch"));

      es.addEventListener("radar.polled", (evt) => {
        const parsed = parsePayload(evt as MessageEvent);
        if (!parsed) return;
        setLiveEvents((prev) => [parsed, ...prev].slice(0, 40));
        onRadarUpdateRef.current?.(parsed);
        void loadAlertHistory();
      });

      es.addEventListener("webhook.tested", (evt) => {
        const parsed = parsePayload(evt as MessageEvent);
        if (!parsed) return;
        setLiveEvents((prev) => [parsed, ...prev].slice(0, 40));
        void loadAlertHistory();
      });

      es.addEventListener("algo.upstream_drift_detected", (evt) => {
        const parsed = parsePayload(evt as MessageEvent);
        if (!parsed) return;
        setLiveEvents((prev) => [parsed, ...prev].slice(0, 40));
        setUnreadCount((c) => c + 1);
      });
    };

    void connect();

    return () => {
      cancelled = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      if (source) source.close();
    };
  }, [workspaceId, loadAlertHistory]);

  const dismissToast = (id: string) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  };

  return (
    <>
      <div className="flex items-center gap-2 flex-wrap">
        {/* Live SSE Pill */}
        <div
          title={
            lastHeartbeat
              ? `SSE stream active · last pulse ${new Date(
                  lastHeartbeat
                ).toLocaleTimeString()}`
              : "Workspace Server-Sent Events stream"
          }
          className={`inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg border text-xs font-medium ${
            connectionState === "live"
              ? "bg-emerald-500/10 border-emerald-500/30 text-emerald-300"
              : connectionState === "connecting"
              ? "bg-sky-500/10 border-sky-500/30 text-sky-300"
              : "bg-amber-500/10 border-amber-500/30 text-amber-300"
          }`}
        >
          {connectionState === "live" ? (
            <>
              <span className="relative flex h-2 w-2">
                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75" />
                <span className="relative inline-flex rounded-full h-2 w-2 bg-emerald-400" />
              </span>
              <Wifi className="h-3.5 w-3.5" />
              <span>LIVE SSE</span>
            </>
          ) : connectionState === "connecting" ? (
            <>
              <Activity className="h-3.5 w-3.5 animate-pulse" />
              <span>SSE Connecting</span>
            </>
          ) : (
            <>
              <WifiOff className="h-3.5 w-3.5" />
              <span>SSE Reconnecting</span>
            </>
          )}
        </div>

        {/* Optional Web Audio API Chime Toggle */}
        <button
          type="button"
          onClick={toggleChime}
          title={
            chimeEnabled
              ? "Web Audio chime active on Watch/Surge breakouts (click to mute)"
              : "Enable Web Audio chime on Watch/Surge breakouts"
          }
          className={`inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg border text-xs font-medium transition-colors ${
            chimeEnabled
              ? "bg-sky-500/15 border-sky-500/40 text-sky-200"
              : "bg-surface-900 border-slate-800 text-slate-400 hover:text-slate-200"
          }`}
        >
          {chimeEnabled ? (
            <>
              <Volume2 className="h-3.5 w-3.5 text-sky-400" />
              <span>Chime On</span>
            </>
          ) : (
            <>
              <VolumeX className="h-3.5 w-3.5" />
              <span>Muted</span>
            </>
          )}
        </button>

        {/* Alert Stream History Drawer Button */}
        <button
          type="button"
          onClick={() => {
            setDrawerOpen((open) => !open);
            setUnreadCount(0);
            void loadAlertHistory();
          }}
          className="relative inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-surface-900 hover:bg-surface-850 border border-slate-800 text-xs font-medium text-slate-200 transition-colors"
        >
          <Bell className="h-3.5 w-3.5 text-amber-400" />
          <span>Alerts ({storedAlerts.length})</span>
          {unreadCount > 0 && (
            <span className="ml-0.5 px-1.5 py-0.2 rounded-full bg-rose-500 text-white text-[10px] font-bold">
              {unreadCount}
            </span>
          )}
        </button>
      </div>

      {/* Slide-in Toast Notifications */}
      {toasts.length > 0 && (
        <div className="fixed bottom-5 right-5 z-50 flex flex-col gap-2.5 max-w-sm w-full pointer-events-none">
          {toasts.map((toast) => {
            const isSurge = toast.tier === "surge";
            return (
              <div
                key={toast.id}
                className={`pointer-events-auto rounded-xl border p-3.5 shadow-2xl backdrop-blur-md transition-all ${
                  isSurge
                    ? "bg-surface-900/95 border-rose-500/50 shadow-rose-950/40"
                    : "bg-surface-900/95 border-amber-500/40 shadow-amber-950/30"
                }`}
              >
                <div className="flex items-start justify-between gap-2">
                  <div className="flex items-center gap-2">
                    <span
                      className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-wider ${
                        isSurge
                          ? "bg-rose-500/20 text-rose-300 border border-rose-500/40"
                          : "bg-amber-500/20 text-amber-300 border border-amber-500/40"
                      }`}
                    >
                      <Flame className="h-3 w-3" />
                      {isSurge ? "Surge Breakout" : "Watch Candidate"}
                    </span>
                    <span className="text-xs font-mono text-white font-semibold">
                      @{toast.authorHandle}
                    </span>
                    <span className="text-xs font-mono text-sky-300">
                      {toast.score.toFixed(1)}
                    </span>
                  </div>
                  <button
                    type="button"
                    onClick={() => dismissToast(toast.id)}
                    className="text-slate-400 hover:text-white"
                  >
                    <X className="h-3.5 w-3.5" />
                  </button>
                </div>
                <p className="text-xs text-slate-300 mt-1.5 line-clamp-2">
                  {toast.textPreview}
                </p>
                {toast.postId !== null && onSelectPostId && (
                  <button
                    type="button"
                    onClick={() => {
                      onSelectPostId(toast.postId as number);
                      dismissToast(toast.id);
                    }}
                    className="mt-2 text-[11px] font-medium text-sky-400 hover:text-sky-300"
                  >
                    Inspect Trajectory &amp; Value-Model Heads &rarr;
                  </button>
                )}
              </div>
            );
          })}
        </div>
      )}

      {/* Alert History Slide-Over Drawer */}
      {drawerOpen && (
        <div className="fixed inset-0 z-40 flex justify-end bg-black/50 backdrop-blur-xs">
          <div className="w-full max-w-md bg-surface-950 border-l border-slate-800 h-full flex flex-col shadow-2xl">
            <div className="px-5 py-4 border-b border-slate-800 flex items-center justify-between">
              <div>
                <h3 className="text-sm font-semibold text-white flex items-center gap-2">
                  <Bell className="h-4 w-4 text-amber-400" />
                  <span>Live SSE &amp; Alert Delivery Feed</span>
                </h3>
                <p className="text-xs text-slate-400 mt-0.5">
                  Deduplicated per-post Watch/Surge transitions &amp; poll pulses
                </p>
              </div>
              <button
                type="button"
                onClick={() => setDrawerOpen(false)}
                className="p-1.5 rounded-lg text-slate-400 hover:text-white hover:bg-surface-850"
              >
                <X className="h-4 w-4" />
              </button>
            </div>

            <div className="flex-1 overflow-y-auto p-4 space-y-4">
              {liveEvents.length > 0 && (
                <div className="space-y-2">
                  <div className="text-[11px] font-semibold uppercase tracking-wider text-emerald-400">
                    Live Session Stream ({liveEvents.length})
                  </div>
                  {liveEvents.map((ev) => (
                    <div
                      key={ev.id}
                      className="rounded-lg bg-surface-900 border border-slate-800 p-3 text-xs space-y-1"
                    >
                      <div className="flex items-center justify-between">
                        <span className="font-mono font-semibold text-sky-300">
                          {ev.event_type}
                        </span>
                        <span className="text-[11px] text-slate-500 font-mono">
                          {new Date(ev.timestamp).toLocaleTimeString()}
                        </span>
                      </div>
                      <div className="text-slate-300 truncate">
                        {ev.event_type === "radar.polled"
                          ? `Polled ${String(
                              ev.data.active_posts_count ?? 0
                            )} active posts · ${String(
                              ev.data.new_alerts_count ?? 0
                            )} new alerts`
                          : String(
                              ev.data.text_preview ||
                                ev.data.structural_summary ||
                                ev.data.note ||
                                `Workspace #${ev.workspace_id} event`
                            )}
                      </div>
                    </div>
                  ))}
                </div>
              )}

              <div className="space-y-2">
                <div className="text-[11px] font-semibold uppercase tracking-wider text-slate-400">
                  Persisted Alert Deliveries ({storedAlerts.length})
                </div>
                {storedAlerts.length === 0 ? (
                  <div className="rounded-xl border border-slate-800/80 bg-surface-900/50 p-6 text-center text-xs text-slate-400">
                    No alert deliveries logged yet for this workspace.
                  </div>
                ) : (
                  storedAlerts.map((item) => {
                    const authorObj = (item.payload_json?.author ??
                      {}) as Record<string, unknown>;
                    const handle = String(
                      authorObj.handle ||
                        item.payload_json?.author_handle ||
                        ""
                    );
                    const score = item.payload_json?.breakout_score;
                    return (
                      <div
                        key={item.id}
                        className="rounded-xl bg-surface-900 border border-slate-800 p-3 text-xs space-y-1.5"
                      >
                        <div className="flex items-center justify-between gap-2">
                          <div className="flex items-center gap-1.5">
                            <span
                              className={`px-1.5 py-0.5 rounded text-[10px] font-semibold uppercase ${
                                item.alert_tier === "surge"
                                  ? "bg-rose-500/20 text-rose-300"
                                  : item.alert_tier === "watch"
                                  ? "bg-amber-500/20 text-amber-300"
                                  : "bg-sky-500/20 text-sky-300"
                              }`}
                            >
                              {item.alert_tier}
                            </span>
                            <span className="font-mono text-slate-300">
                              {item.channel.toUpperCase()}
                            </span>
                            {handle && (
                              <span className="font-mono text-white">
                                @{handle}
                              </span>
                            )}
                          </div>
                          <span
                            className={`inline-flex items-center gap-1 text-[11px] font-mono ${
                              item.status === "delivered"
                                ? "text-emerald-400"
                                : item.status.startsWith("suppressed")
                                ? "text-amber-400"
                                : "text-rose-400"
                            }`}
                          >
                            {item.status === "delivered" && (
                              <CheckCircle2 className="h-3 w-3" />
                            )}
                            {item.status}
                          </span>
                        </div>
                        <div className="flex items-center justify-between text-[11px] text-slate-400">
                          <span>
                            {typeof score === "number"
                              ? `Score ${score.toFixed(1)}`
                              : item.event_type}
                            {item.http_status ? ` · HTTP ${item.http_status}` : ""}
                          </span>
                          <span className="font-mono">
                            {new Date(item.created_at).toLocaleTimeString()}
                          </span>
                        </div>
                        {item.post_id && onSelectPostId && (
                          <button
                            type="button"
                            onClick={() => {
                              onSelectPostId(item.post_id as number);
                              setDrawerOpen(false);
                            }}
                            className="text-[11px] text-sky-400 hover:text-sky-300 font-medium"
                          >
                            Inspect Post #{item.post_id} &rarr;
                          </button>
                        )}
                      </div>
                    );
                  })
                )}
              </div>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
