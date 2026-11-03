import type { Metadata } from "next";
import "./globals.css";
import { WorkspaceProvider } from "@/components/workspace-context";
import { AppShell } from "@/components/app-shell";

export const metadata: Metadata = {
  title: "TweetTracker — Hot Posts & X Network",
  description:
    "Live queue of breakout X posts to reply or quote and a self-organizing map of the people in your network.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" className="dark">
      <body className="antialiased">
        <WorkspaceProvider>
          <AppShell>{children}</AppShell>
        </WorkspaceProvider>
      </body>
    </html>
  );
}
