import * as React from "react";
import { SidebarProvider, SidebarInset } from "../sidebar";
import { cn } from "../../lib/utils";

export interface AppShellProps {
  sidebar: React.ReactNode;
  topbar?: React.ReactNode;
  children: React.ReactNode;
  /** Wrap main content with a max-width container. Set to false for full-bleed layouts. */
  contained?: boolean;
  /**
   * Whether <main> owns the vertical scroll (overflow-y-auto). True for document
   * pages that scroll as a whole. Set false ONLY for a page that pins its own
   * header/footer and manages an inner scroll region (e.g. the Chat composer);
   * main then clips (overflow-hidden) and hands the child a fixed-height box.
   * Independent of `contained`. Defaults true so a page never silently loses
   * its scroll.
   */
  scroll?: boolean;
  className?: string;
}

/**
 * Shared application shell. Renders a `<SidebarProvider>` with a sidebar slot,
 * an optional top bar, and a scrollable main content region. Used by both
 * apps/web (the desktop app) and web (the code app) so the chrome is
 * identical and product-specific nav config is what changes.
 */
export function AppShell({
  sidebar,
  topbar,
  children,
  contained = true,
  scroll = true,
  className,
}: AppShellProps) {
  return (
    <SidebarProvider>
      {sidebar}
      <SidebarInset className={cn("min-w-0 bg-background", className)}>
        {topbar}
        {/* `scroll` owns the vertical-scroll decision (NOT `contained`): a
            document page scrolls as a whole (overflow-y-auto); a page that pins
            its own header/footer + manages an inner scroll region (e.g. Chat)
            passes scroll=false so main clips (overflow-hidden) and hands a
            fixed-height (min-h-0) box to the child, otherwise main scrolling
            would drag the pinned chrome. `contained` only adds the max-w
            centering wrapper. */}
        <main className={cn("relative flex-1 min-h-0",
                            scroll ? "overflow-y-auto" : "overflow-hidden")}>
          {contained ? (
            <div className="mx-auto w-full max-w-6xl px-4 py-6 sm:px-6 sm:py-8 lg:px-10 lg:py-10">
              {children}
            </div>
          ) : (
            children
          )}
        </main>
      </SidebarInset>
    </SidebarProvider>
  );
}
