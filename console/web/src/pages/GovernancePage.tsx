import { useNavigate, useParams } from 'react-router-dom';
import { cn } from '@foxl/ui';
import { SectionHeader } from '../shared';
import { GOV_SECTIONS, govSection } from './governance/sections';
import { OverviewSection } from './governance/OverviewSection';
import { RuntimesSection } from './governance/RuntimesSection';
import { SessionsSection } from './governance/SessionsSection';
import { CostSection } from './governance/CostSection';
import { AuditSection } from './governance/AuditSection';
import { PoliciesSection } from './governance/PoliciesSection';
import { AnalyzeSection } from './governance/AnalyzeSection';

/**
 * The governance mini-dashboard. The section nav lives in the app's LEFT
 * SIDEBAR (GovernanceSubNav, nested under the Governance item) and drives the
 * URL, /governance/<section>. This page reads that segment and renders the
 * matching section. On narrow screens, where the sidebar collapses to icons, a
 * horizontal chip row stands in for the nav.
 */
export function GovernancePage() {
  const { section } = useParams<{ section?: string }>();
  const navigate = useNavigate();
  const current = govSection(section);

  return (
    <div className="animate-enter-up mx-auto flex w-full max-w-6xl flex-col gap-6 px-6 py-8">
      {/* Small-screen fallback nav (the sidebar sub-nav is hidden when collapsed). */}
      <div className="-mx-1 flex gap-1.5 overflow-x-auto px-1 md:hidden">
        {GOV_SECTIONS.map((s) => {
          const on = s.id === current.id;
          return (
            <button
              key={s.id}
              onClick={() => navigate(`/governance/${s.id}`)}
              className={cn(
                'shrink-0 rounded-full px-3 py-1.5 text-xs font-medium transition-colors',
                on ? 'bg-primary text-primary-foreground' : 'bg-muted text-muted-foreground',
              )}
            >
              {s.label}
            </button>
          );
        })}
      </div>

      <SectionHeader title={current.title} subtitle={current.subtitle} source="ledger" />

      {current.id === 'overview' && <OverviewSection />}
      {current.id === 'runtimes' && <RuntimesSection />}
      {current.id === 'sessions' && <SessionsSection />}
      {current.id === 'cost' && <CostSection />}
      {current.id === 'audit' && <AuditSection />}
      {current.id === 'policies' && <PoliciesSection />}
      {current.id === 'analyze' && <AnalyzeSection />}
    </div>
  );
}
