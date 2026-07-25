import { useEffect, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { cn } from '@foxl/ui';
import { AgentIcon } from './AgentIcon';
import { onAgentRoles, defaultAgentRole, type AgentRole } from '../pages/agents/environments';

/**
 * The coding-agent roles this deployment serves, rendered inline under the
 * "Agents" nav item in the app's left sidebar, the same inline-list pattern
 * GovernanceSubNav uses under "Governance". Each row deep-links to
 * /agents/<role>; the active row is highlighted. Only shown while on an /agents
 * route so the sidebar stays calm elsewhere.
 *
 * The rows come from the served roster, so the sidebar lists exactly the team the
 * engine can dispatch, however many that is.
 */
export function AgentsSubNav() {
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const [roles, setRoles] = useState<AgentRole[]>([]);
  useEffect(() => onAgentRoles(setRoles), []);
  if (!pathname.startsWith('/agents')) return null;

  const seg = pathname.split('/')[2] || defaultAgentRole();

  return (
    // Indented under the Agents item, sized to match the main nav rows (text-sm +
    // size-4 icon + py-1.5) so the sub-nav reads as first-class navigation.
    <div className="ml-4 mr-1 mt-0.5 flex flex-col gap-0.5 border-l border-sidebar-border/60 pl-3">
      {roles.map((e) => {
        const active = e.id === seg;
        return (
          <button
            key={e.id}
            onClick={() => navigate(`/agents/${e.id}`)}
            title={e.blurb}
            className={cn(
              'flex items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm transition-colors',
              active
                ? 'bg-sidebar-accent font-medium text-sidebar-accent-foreground'
                : 'text-muted-foreground hover:bg-sidebar-accent/50 hover:text-foreground',
            )}
          >
            <AgentIcon agentId={e.id} size={16} />
            <span className="truncate">{e.label}</span>
          </button>
        );
      })}
    </div>
  );
}
