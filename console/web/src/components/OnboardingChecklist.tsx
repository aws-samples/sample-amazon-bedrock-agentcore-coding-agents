import { Link } from 'react-router-dom';
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, Button,
} from '@foxl/ui';
import {
  SquareTerminal, Bot, Boxes, ShieldCheck, Settings, ChevronRight, ArrowRight,
} from 'lucide-react';
import { useOnboarding } from '../hooks/useOnboarding';

const AREAS = [
  { icon: SquareTerminal, name: 'Development', blurb: 'Live shell for writing code and running commands' },
  { icon: Bot, name: 'Agents', blurb: 'Build and deploy the three coding agents' },
  { icon: Boxes, name: 'Tasks', blurb: 'Orchestrate a build, review, and open a PR' },
  { icon: ShieldCheck, name: 'Governance', blurb: 'Per-user cost, identity, guardrails' },
];

const FLOW = ['Build', 'Deploy', 'Orchestrate', 'Govern'];

export function OnboardingModal() {
  const { open, dismiss } = useOnboarding();

  return (
    <Dialog open={open} onOpenChange={(o) => { if (!o) dismiss(); }}>
      <DialogContent
        slideFrom="center"
        overlayClassName="bg-background"
        className="flex max-w-md flex-col gap-4 overflow-hidden"
      >
        <DialogHeader className="text-left">
          <DialogTitle>AgentCore Console</DialogTitle>
          <DialogDescription>
            Run coding agents on AgentCore Runtime. ~4 hours total.
          </DialogDescription>
        </DialogHeader>

        <div
          className="flex items-center gap-1.5 text-[11px] font-medium uppercase tracking-wide text-muted-foreground"
          aria-hidden
        >
          {FLOW.map((label, i) => (
            <span key={label} className="contents">
              <span className="min-w-0 flex-1 truncate text-center">{label}</span>
              {i < FLOW.length - 1 && <ChevronRight className="size-3 shrink-0 text-muted-foreground/50" />}
            </span>
          ))}
        </div>

        <ul role="list" className="space-y-1.5">
          {AREAS.map((area) => (
            <li key={area.name} className="flex items-center gap-3 rounded-lg border border-border bg-card px-3 py-2">
              <span className="flex size-7 shrink-0 items-center justify-center rounded-md bg-muted text-foreground">
                <area.icon className="size-3.5" />
              </span>
              <div className="min-w-0 flex-1">
                <span className="text-sm font-medium text-foreground">{area.name}</span>
                <p className="mt-0.5 text-xs text-muted-foreground">{area.blurb}</p>
              </div>
            </li>
          ))}
        </ul>

        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          <Settings className="size-3.5 shrink-0" />
          <span>Settings: GitHub Gateway, runtimes, merge policy.</span>
        </div>

        <div className="flex items-center justify-between">
          <p className="text-[11px] text-muted-foreground">Reopen from sidebar "Setup guide"</p>
          <div className="flex items-center gap-2">
            <Button variant="ghost" size="sm" onClick={dismiss}>Skip</Button>
            <Button asChild size="sm">
              <Link to="/development" onClick={dismiss}>
                Start
                <ArrowRight className="ml-1 size-3.5" />
              </Link>
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
