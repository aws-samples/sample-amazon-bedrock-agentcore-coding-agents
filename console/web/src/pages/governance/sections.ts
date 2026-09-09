import {
  LayoutDashboard, Cpu, ListTree, DollarSign, ScrollText, ShieldCheck, Sparkles, Fingerprint,
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
  { id: 'overview', label: 'Overview', icon: LayoutDashboard, sub: 'Fleet health at a glance', title: 'Governance', subtitle: "Inspect sessions observed by this console and the usage estimates in its host ledger." },
  { id: 'runtimes', label: 'Runtimes', icon: Cpu, sub: 'Deployed fleet & live probe', title: 'Runtimes', subtitle: 'See the deployed AgentCore Runtimes, their wiring, fleet size, and the result of a one-line probe inside a Runtime.' },
  { id: 'sessions', label: 'Sessions', icon: ListTree, sub: 'See and stop active sessions', title: 'Sessions', subtitle: 'See who started each Runtime session, open its details, or stop it when it is no longer needed.' },
  { id: 'attribution', label: 'Attribution', icon: Fingerprint, sub: 'Lab 3 · exported user labels', title: 'Attribution', subtitle: 'Trace a user label from an agent session into real CloudWatch request events.' },
  { id: 'cost', label: 'Cost', icon: DollarSign, sub: 'Recorded usage estimates', title: 'Cost estimates', subtitle: 'Group the recorded usage estimates by role or user. Check the source and coverage before treating them as a bill.' },
  { id: 'audit', label: 'Audit', icon: ScrollText, sub: 'Append-only ledger trail', title: 'Audit trail', subtitle: 'Every recorded ledger event appears as one auditable line.' },
  { id: 'policies', label: 'Guardrails', icon: ShieldCheck, sub: 'Workshop action screening', title: 'Guardrails', subtitle: 'Inspect the action patterns screened by the workshop harness and the boundaries of that screening.' },
  { id: 'analyze', label: 'Analyze', icon: Sparkles, sub: 'Ask the model about this fleet', title: 'AI analysis', subtitle: 'Ask the orchestrator model about this fleet, grounded in the live governance snapshot.' },
];

export const DEFAULT_GOV_SECTION = 'overview';

export function govSection(id: string | undefined): GovSection {
  return GOV_SECTIONS.find((s) => s.id === id) ?? GOV_SECTIONS[0]!;
}
