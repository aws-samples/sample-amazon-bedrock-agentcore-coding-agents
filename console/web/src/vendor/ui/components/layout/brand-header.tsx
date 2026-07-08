import * as React from "react";
import { cn } from "../../lib/utils";

export interface BrandHeaderProps {
  /** Brand mark - typically a logo SVG sized to ~h-7 w-7 */
  mark: React.ReactNode;
  /** Product name shown next to the mark */
  name: string;
  /** Subtitle/tagline shown under the name when expanded */
  tagline?: string;
  /** Match shadcn sidebar's collapsed state - hides text when sidebar collapses */
  collapseOnSidebarIcon?: boolean;
  className?: string;
}

/**
 * Shared sidebar brand header. Used by apps/web (the desktop app) and
 * web (the code app) so both apps render the same brand block at the
 * top of the sidebar regardless of product-specific nav below.
 */
export function BrandHeader({
  mark,
  name,
  tagline,
  collapseOnSidebarIcon = true,
  className,
}: BrandHeaderProps) {
  return (
    <div className={cn("flex items-center gap-2.5 px-1", className)}>
      <div className="shrink-0">{mark}</div>
      <div
        className={cn(
          "flex min-w-0 flex-col leading-tight",
          collapseOnSidebarIcon && "group-data-[collapsible=icon]:hidden",
        )}
      >
        <span className="truncate text-sm font-semibold tracking-tight text-sidebar-foreground">
          {name}
        </span>
        {tagline && (
          <span className="truncate text-[11px] font-normal text-sidebar-foreground/60">
            {tagline}
          </span>
        )}
      </div>
    </div>
  );
}
