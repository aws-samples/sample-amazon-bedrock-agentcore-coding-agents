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
  { icon: Bot, name: 'Agents', blurb: 'Inspect each role and open its Runtime shell' },
  { icon: Boxes, name: 'Chat', blurb: 'Submit a goal, follow the work, and inspect PR evidence' },
  { icon: ShieldCheck, name: 'Governance', blurb: 'Inspect sessions, exported usage, and attribution' },
];

const FLOW = ['Host', 'Coordinate', 'Verify', 'Attribute'];

export function OnboardingModal() {
  const { open, dismiss } = useOnboarding();

  return (
    <Dialog open={open} onOpenChange={(o) => { if (!o) dismiss(); }}>
      <DialogContent
        slideFrom="center"
        className="flex max-w-lg flex-col gap-5 overflow-hidden p-7"
      >
        <DialogHeader className="text-left">
          <div className="eyebrow mb-2">Your workshop companion</div>
          <DialogTitle className="text-2xl">A team you can inspect.</DialogTitle>
          <DialogDescription>
            Host the agents, coordinate a build, and read the evidence before accepting
            a change. The guided workshop is two hours, after Getting Started.
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
            <li key={area.name} className="flex items-center gap-3 rounded-xl border border-border bg-card px-4 py-3">
              <span className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-success/8 text-success">
                <area.icon aria-hidden="true" className="size-[18px]" />
              </span>
              <div className="min-w-0 flex-1">
                <span className="text-sm font-medium text-foreground">{area.name}</span>
                <p className="mt-0.5 text-xs text-muted-foreground">{area.blurb}</p>
              </div>
            </li>
          ))}
        </ul>

        <p className="rounded-lg border border-border bg-muted/40 p-3 text-xs leading-relaxed text-muted-foreground">
          Following the CLI lab? Keep watching that run from your terminal.
          This host console starts separate chats and builds; its history does
          not include runs submitted to the deployed coordinator.
        </p>
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
                Open Development
                <ArrowRight className="ml-1 size-3.5" />
              </Link>
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
