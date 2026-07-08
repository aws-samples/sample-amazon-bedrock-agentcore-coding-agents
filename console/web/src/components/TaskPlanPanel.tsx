import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Badge } from '@foxl/ui';
import { AgentIcon } from './AgentIcon';
import { PulseDot } from './Motion';
import { listAgents, type Agent } from '../api';

/**
 * Right-hand fleet panel. Lists the coding agents that have been deployed to
 * AgentCore Runtime (status === "ready", runtime_arn present). Polls listAgents
 * every 3 s so new deploys appear without a page reload.
 *
 * Rows are clickable: clicking an agent navigates to /agents (the Agents page
 * where you can open a shell and manage that agent).
 *
 * Empty state: if nothing is deployed yet, shows a prompt to deploy one.
 * Real data only: every row comes from a real listAgents() response.
 */
export function TaskPlanPanel({
  conversationRunIds: _conversationRunIds,
  onSelect: _onSelect,
}: {
  conversationRunIds: string[];
  onSelect?: (runId: string) => void;
}) {
  const [agents, setAgents] = useState<Agent[]>([]);
  const navigate = useNavigate();

  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      try {
        const all = await listAgents();
        if (!cancelled) setAgents(all);
      } catch { /* keep what we have */ }
    };
    tick();
    const t = setInterval(tick, 3000);
    return () => { cancelled = true; clearInterval(t); };
  }, []);

  // Only agents that have actually been deployed to a runtime.
  const ready = agents.filter((a) => a.status === 'ready' && a.runtime_arn);
  const pending = agents.filter((a) => a.status !== 'ready' || !a.runtime_arn);

  return (
    <div className="flex h-full flex-col">
      <div className="border-b border-border px-4 py-3">
        <div className="eyebrow text-muted-foreground">Fleet</div>
        <h2 className="mt-0.5 text-sm font-semibold tracking-tight">Coding agents on Runtime</h2>
      </div>

      <div className="flex-1 space-y-4 overflow-y-auto px-3 py-3">
        {agents.length === 0 && (
          <p className="px-1 py-6 text-center text-xs text-muted-foreground">
            No agents deployed. Deploy one in the Agents page.
          </p>
        )}

        {ready.length > 0 && (
          <AgentSection
            label="READY"
            agents={ready}
            onNavigate={() => navigate('/agents')}
          />
        )}

        {pending.length > 0 && (
          <AgentSection
            label="PENDING"
            agents={pending}
            onNavigate={() => navigate('/agents')}
          />
        )}
      </div>
    </div>
  );
}

function AgentSection({
  label, agents, onNavigate,
}: {
  label: string;
  agents: Agent[];
  onNavigate: () => void;
}) {
  return (
    <div className="space-y-1">
      <div className="eyebrow px-1 text-muted-foreground/60">
        {label}
      </div>
      {agents.map((a) => (
        <AgentRow key={a.agent_id} agent={a} onNavigate={onNavigate} />
      ))}
    </div>
  );
}

function AgentRow({ agent, onNavigate }: { agent: Agent; onNavigate: () => void }) {
  const displayName = agent.name || agent.label;
  const isReady = agent.status === 'ready' && !!agent.runtime_arn;
  // Truncate the ARN to its last segment for legibility.
  const arnShort = agent.runtime_arn
    ? agent.runtime_arn.split('/').pop() ?? agent.runtime_arn.slice(-20)
    : null;

  return (
    <button
      type="button"
      onClick={onNavigate}
      title={`${displayName}: open in Agents`}
      className="animate-enter-up flex w-full items-start gap-2.5 rounded-md px-2 py-2 text-left transition-colors hover:bg-muted/60"
    >
      <AgentIcon agentId={agent.agent_id} size={20} />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-1.5">
          <span className="truncate text-xs font-medium text-foreground">{displayName}</span>
          <Badge
            variant={isReady ? 'success' : 'outline'}
            className="ml-auto shrink-0 gap-1 text-[10px] px-1.5 py-0"
          >
            {isReady && <PulseDot tone="success" size={5} />}
            {isReady ? 'ready' : agent.status}
          </Badge>
        </div>
        {arnShort && (
          <div className="mt-0.5 truncate font-mono text-[10px] text-muted-foreground/70">
            {arnShort}
          </div>
        )}
        {agent.purpose && (
          <div className="mt-0.5 truncate text-[11px] text-muted-foreground">
            {agent.purpose}
          </div>
        )}
      </div>
    </button>
  );
}
