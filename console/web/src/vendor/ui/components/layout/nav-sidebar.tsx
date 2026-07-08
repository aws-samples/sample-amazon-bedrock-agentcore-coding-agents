import * as React from "react";
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarRail,
} from "../sidebar";
import { Badge } from "../badge";
import { cn } from "../../lib/utils";

export interface NavSidebarItem {
  id: string;
  label: string;
  icon: React.ComponentType<{ className?: string }>;
  /** When true the item renders as active. */
  isActive?: boolean;
  /** Click handler. Provide either onSelect or href. */
  onSelect?: () => void;
  /** Renders an `<a>` instead of a `<button>` when set. */
  href?: string;
  /** Optional badge count rendered on the right. */
  badge?: number | string;
  /** Tooltip shown when the sidebar is collapsed. Defaults to label. */
  tooltip?: string;
  /** Optional right-aligned ReactNode (e.g. spinner, icon). */
  trailing?: React.ReactNode;
  /** Optional content rendered directly below this row (e.g. an inline list of
   *  recent items that scrolls under the nav item). */
  after?: React.ReactNode;
}

export interface NavSidebarGroup {
  label?: string;
  items: NavSidebarItem[];
}

export interface NavSidebarProps {
  /** Top-of-sidebar header. Typically `<BrandHeader>`. */
  header?: React.ReactNode;
  /** Primary nav groups rendered in order. */
  groups: NavSidebarGroup[];
  /** Footer nav (e.g. settings, account). */
  footer?: React.ReactNode;
  /** shadcn collapsible mode. */
  collapsible?: "offcanvas" | "icon" | "none";
  className?: string;
}

/**
 * Configurable shadcn sidebar. Consumers describe their nav with
 * `NavSidebarGroup[]` and pass header/footer slots; no product-specific
 * logic lives in this component.
 */
export function NavSidebar({
  header,
  groups,
  footer,
  collapsible = "icon",
  className,
}: NavSidebarProps) {
  return (
    <Sidebar collapsible={collapsible} className={cn("border-r", className)}>
      {header && (
        <SidebarHeader className="border-b border-sidebar-border/60 px-4 py-3 group-data-[collapsible=icon]:px-2 group-data-[collapsible=icon]:py-2">
          {header}
        </SidebarHeader>
      )}
      <SidebarContent className="px-2 group-data-[collapsible=icon]:px-0">
        {groups.map((group, groupIdx) => (
          <SidebarGroup
            key={group.label || `group-${groupIdx}`}
            className="py-1"
          >
            {group.label && (
              <SidebarGroupLabel className="px-2 text-[11px] font-medium text-muted-foreground/60">
                {group.label}
              </SidebarGroupLabel>
            )}
            <SidebarGroupContent>
              <SidebarMenu>
                {group.items.map((item) => (
                  <NavSidebarRow key={item.id} item={item} />
                ))}
              </SidebarMenu>
            </SidebarGroupContent>
          </SidebarGroup>
        ))}
      </SidebarContent>
      {footer && (
        <SidebarFooter className="border-t border-sidebar-border/60 px-2 group-data-[collapsible=icon]:px-0">
          {footer}
        </SidebarFooter>
      )}
      <SidebarRail />
    </Sidebar>
  );
}

function NavSidebarRow({ item }: { item: NavSidebarItem }) {
  const Icon = item.icon;
  const trailing = item.trailing ?? null;
  const badge =
    item.badge !== undefined && item.badge !== 0 ? (
      <Badge
        variant="secondary"
        className="ml-auto h-5 min-w-5 justify-center px-1 text-[10px] tabular-nums"
      >
        {typeof item.badge === "number" && item.badge > 99 ? "99+" : item.badge}
      </Badge>
    ) : null;

  const inner = (
    <>
      <Icon className="h-4 w-4" />
      <span>{item.label}</span>
      {badge}
      {trailing}
    </>
  );

  if (item.href) {
    return (
      <SidebarMenuItem>
        <SidebarMenuButton
          asChild
          isActive={item.isActive}
          tooltip={item.tooltip ?? item.label}
        >
          <a href={item.href}>{inner}</a>
        </SidebarMenuButton>
      </SidebarMenuItem>
    );
  }
  return (
    <SidebarMenuItem>
      <SidebarMenuButton
        isActive={item.isActive}
        onClick={item.onSelect}
        tooltip={item.tooltip ?? item.label}
        className="w-full"
      >
        {inner}
      </SidebarMenuButton>
      {/* Inline content under the row (e.g. an infinite-scroll list). Hidden when
          the sidebar is collapsed to icons, where there is no room for it. */}
      {item.after && (
        <div className="group-data-[collapsible=icon]:hidden">{item.after}</div>
      )}
    </SidebarMenuItem>
  );
}
