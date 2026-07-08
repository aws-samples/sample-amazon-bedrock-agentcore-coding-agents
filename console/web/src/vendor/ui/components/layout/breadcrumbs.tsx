import * as React from "react";
import { cn } from "../../lib/utils";

export interface BreadcrumbCrumb {
  label: string;
  /** When provided the crumb renders as a link. */
  to?: string;
}

export interface BreadcrumbsProps {
  crumbs: BreadcrumbCrumb[];
  /**
   * Optional link renderer. Pass a router-aware Link if you want client-side
   * navigation; falls back to a plain `<a>` otherwise.
   */
  linkAs?: React.ComponentType<{ to: string; className?: string; children: React.ReactNode }>;
  className?: string;
}

/**
 * Plain breadcrumb list. Last crumb renders as foreground text (no link).
 * Empty list renders nothing.
 */
export function Breadcrumbs({ crumbs, linkAs, className }: BreadcrumbsProps) {
  if (crumbs.length === 0) return null;
  const Link = linkAs;
  return (
    <nav
      aria-label="Breadcrumb"
      className={cn("flex items-center gap-1.5 overflow-hidden text-sm", className)}
    >
      {crumbs.map((c, i) => {
        const isLast = i === crumbs.length - 1;
        const cls = cn(
          "truncate transition-colors",
          isLast
            ? "font-medium text-foreground"
            : "text-muted-foreground hover:text-foreground",
        );
        return (
          <span key={(c.to ?? c.label) + ":" + i} className="flex items-center gap-1.5 truncate">
            {i > 0 && <span className="text-muted-foreground/50">/</span>}
            {c.to && !isLast ? (
              Link ? (
                <Link to={c.to} className={cls}>
                  {c.label}
                </Link>
              ) : (
                <a href={c.to} className={cls}>
                  {c.label}
                </a>
              )
            ) : (
              <span className={cls}>{c.label}</span>
            )}
          </span>
        );
      })}
    </nav>
  );
}

/**
 * Compute breadcrumbs from a pathname. Splits on `/`, humanizes each segment,
 * shortens long ID-like trailing segments, and marks the last crumb as
 * non-linkable. Pass `rootLabel` to override the home crumb (default "Home").
 */
export function breadcrumbsFromPath(
  pathname: string,
  rootLabel = "Home",
): BreadcrumbCrumb[] {
  if (pathname === "/" || pathname === "") {
    return [{ label: rootLabel }];
  }
  const parts = pathname.split("/").filter(Boolean);
  const out: BreadcrumbCrumb[] = [];
  let acc = "";
  for (let i = 0; i < parts.length; i++) {
    const seg = parts[i] ?? "";
    acc += "/" + seg;
    const isLast = i === parts.length - 1;
    out.push({
      label: humanize(seg),
      to: isLast ? undefined : acc,
    });
  }
  return out;
}

function humanize(seg: string): string {
  if (seg.length > 12 && /^[a-z0-9]+$/i.test(seg)) {
    return seg.slice(0, 8);
  }
  return seg.charAt(0).toUpperCase() + seg.slice(1);
}
