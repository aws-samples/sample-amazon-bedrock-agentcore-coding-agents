import { useLocation, useNavigate } from 'react-router-dom';
import { cn } from '@foxl/ui';
import { GOV_SECTIONS, DEFAULT_GOV_SECTION } from '../pages/governance/sections';

/**
 * The governance sections, rendered inline under the "Governance" nav item in
 * the app's left sidebar, the same inline-list pattern SidebarRunList uses
 * under "Tasks". Each row deep-links to /governance/<section>; the active row is
 * highlighted. Only shown while on a /governance route so the sidebar stays calm
 * elsewhere.
 */
export function GovernanceSubNav() {
  const navigate = useNavigate();
  const { pathname } = useLocation();
  if (!pathname.startsWith('/governance')) return null;

  const seg = pathname.split('/')[2] || DEFAULT_GOV_SECTION;

  return (
    // Indented under the Governance item, but sized to match the main nav rows
    // (text-sm + size-4 icon + the same py-1.5 row height) so the sub-nav reads
    // as first-class navigation, not a cramped footnote.
    <div className="ml-4 mr-1 mt-0.5 flex flex-col gap-0.5 border-l border-sidebar-border/60 pl-3">
      {GOV_SECTIONS.map((s) => {
        const Icon = s.icon;
        const active = s.id === seg;
        return (
          <button
            key={s.id}
            onClick={() => navigate(`/governance/${s.id}`)}
            title={s.sub}
            className={cn(
              'flex items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm transition-colors',
              active
                ? 'bg-sidebar-accent font-medium text-sidebar-accent-foreground'
                : 'text-muted-foreground hover:bg-sidebar-accent/50 hover:text-foreground',
            )}
          >
            <Icon className="size-4 shrink-0" />
            <span className="truncate">{s.label}</span>
          </button>
        );
      })}
    </div>
  );
}
