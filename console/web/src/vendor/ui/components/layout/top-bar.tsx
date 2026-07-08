import * as React from "react";
import { SidebarTrigger } from "../sidebar";
import { Separator } from "../separator";
import { cn } from "../../lib/utils";

export interface TopBarProps {
  /** Left-side content rendered after the sidebar trigger (e.g. breadcrumbs). */
  children?: React.ReactNode;
  /** Right-aligned actions (buttons, badges, menus). */
  actions?: React.ReactNode;
  className?: string;
}

/**
 * Sticky application top bar. Always renders the sidebar trigger; consumers
 * fill the breadcrumb and action slots.
 */
export function TopBar({ children, actions, className }: TopBarProps) {
  return (
    <header
      className={cn(
        "sticky top-0 z-20 flex h-14 shrink-0 items-center gap-3 border-b border-border/60 bg-background/85 px-4 backdrop-blur supports-[backdrop-filter]:bg-background/60 lg:px-6",
        className,
      )}
    >
      <SidebarTrigger className="-ml-1 text-muted-foreground hover:text-foreground" />
      <Separator orientation="vertical" className="mr-1 h-5" />
      <div className="flex min-w-0 flex-1 items-center gap-2">{children}</div>
      {actions && <div className="ml-auto flex items-center gap-2">{actions}</div>}
    </header>
  );
}
