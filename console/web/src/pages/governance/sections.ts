import {
  LayoutDashboard, Cpu, ListTree, DollarSign, ScrollText, ShieldCheck, Sparkles,
  type LucideIcon,
} from 'lucide-react';

// The governance dashboard's sections: the single source of truth shared by the
// left-sidebar sub-nav (App shell) and the page itself. Each answers one question a
// security review asks of an agent fleet: who acted, what it cost, what it did, and
// what bounded it, plus an AI lens over the whole snapshot. The `id` is the URL
// segment (/governance/<id>); `overview` is the default when no segment is present.
export interface GovSection {
  id: string;
  label: string;
  icon: LucideIcon;
  sub: string;
  title: string;
  subtitle: string;
}

export const GOV_SECTIONS: GovSection[] = [
  { id: 'overview', label: 'Overview', icon: LayoutDashboard, sub: 'Fleet health at a glance', title: 'Governance', subtitle: "Every number is computed from this machine's real run ledger over the metrics API." },
  { id: 'runtimes', label: 'Runtimes', icon: Cpu, sub: 'Deployed fleet & live probe', title: 'Runtimes', subtitle: 'The deployed AgentCore runtimes the fleet dispatches to: wiring, fleet size, and a live probe that runs a real job inside a runtime.' },
  { id: 'sessions', label: 'Sessions', icon: ListTree, sub: 'Inventory, attribution & kill switch', title: 'Sessions', subtitle: 'Every Runtime session, its recorded submitter, and a kill switch. Click a row for evidence.' },
  { id: 'cost', label: 'Cost', icon: DollarSign, sub: 'Per-agent & per-user spend', title: 'Cost', subtitle: 'The per-user cost surface groups ledger records by agent or authenticated submitter.' },
  { id: 'audit', label: 'Audit', icon: ScrollText, sub: 'Append-only ledger trail', title: 'Audit trail', subtitle: 'Every real ledger event as one auditable line. Nothing synthesized.' },
  { id: 'policies', label: 'Guardrails', icon: ShieldCheck, sub: 'Cedar rules the harness enforces', title: 'Guardrails', subtitle: 'The rule set the harness screens every action against, decided in code, not from a dashboard.' },
  { id: 'analyze', label: 'Analyze', icon: Sparkles, sub: 'Ask the model about this fleet', title: 'AI analysis', subtitle: 'Ask the orchestrator model about this fleet, grounded in the live governance snapshot.' },
];

export const DEFAULT_GOV_SECTION = 'overview';

export function govSection(id: string | undefined): GovSection {
  return GOV_SECTIONS.find((s) => s.id === id) ?? GOV_SECTIONS[0]!;
}
