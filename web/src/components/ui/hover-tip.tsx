"use client";

import * as Tooltip from "@radix-ui/react-tooltip";
import type { ReactNode } from "react";

interface HoverTipProps {
  children: ReactNode;
  content: ReactNode;
  side?: "top" | "right" | "bottom" | "left";
  align?: "start" | "center" | "end";
  maxWidthClass?: string;
}

export function HoverTip({
  children,
  content,
  side = "top",
  align = "center",
  maxWidthClass = "max-w-xs",
}: HoverTipProps) {
  return (
    <Tooltip.Provider delayDuration={120} skipDelayDuration={250}>
      <Tooltip.Root>
        <Tooltip.Trigger asChild>{children}</Tooltip.Trigger>
        <Tooltip.Portal>
          <Tooltip.Content
            side={side}
            align={align}
            sideOffset={6}
            className={`z-50 ${maxWidthClass} rounded-xl border border-slate-700/90 bg-slate-950/95 px-3.5 py-2.5 text-xs text-slate-200 shadow-2xl shadow-black/70 backdrop-blur-md animate-in fade-in-0 zoom-in-95`}
          >
            {content}
            <Tooltip.Arrow className="fill-slate-800" />
          </Tooltip.Content>
        </Tooltip.Portal>
      </Tooltip.Root>
    </Tooltip.Provider>
  );
}
