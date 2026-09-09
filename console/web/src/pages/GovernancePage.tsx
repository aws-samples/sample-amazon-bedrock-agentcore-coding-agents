import { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import ContentLayout from '@cloudscape-design/components/content-layout';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Tabs from '@cloudscape-design/components/tabs';
import { SectionHeader } from '../shared';
import { AgentSessions } from './agents/AgentSessions';
import { AttributionSection } from './governance/AttributionSection';
import { AuditSection } from './governance/AuditSection';
import { ControlsOverview } from './governance/ControlsOverview';
import { PolicyChecker } from './governance/PolicyChecker';

type Section = 'usage' | 'controls' | 'activity';
const SECTIONS = {
  usage: { title: 'Usage', description: 'Attribute exported request and token usage to the people who started the work.' },
  controls: { title: 'Controls', description: 'Inspect identity, approvals, and execution limits. Test an action before it runs.' },
  activity: { title: 'Activity', description: 'Review recorded operations and manage Runtime sessions on this host.' },
};

export function GovernancePage({ section }: { section: Section }) {
  const [search, setSearch] = useSearchParams();
  const activeTab = search.get('tab') === 'sessions' ? 'sessions' : 'audit';
  // Preserve completed query evidence when moving to Controls or Activity.
  // Unvisited sections do not fetch until the person opens them.
  const [visited, setVisited] = useState<Set<Section>>(() => new Set([section]));
  useEffect(() => { setVisited(current => current.has(section) ? current : new Set([...current, section])); }, [section]);
  const [activityVersion, setActivityVersion] = useState(0);
  return <ContentLayout header={<SectionHeader title={SECTIONS[section].title} subtitle={SECTIONS[section].description} />}>
    {(visited.has('usage') || section === 'usage') && <div hidden={section !== 'usage'}><AttributionSection /></div>}
    {section === 'controls' && <SpaceBetween size="l">
      <ControlsOverview /><PolicyChecker onEvaluation={() => setActivityVersion(value => value + 1)} />
    </SpaceBetween>}
    {section === 'activity' &&
      <Tabs activeTabId={activeTab} onChange={({ detail }) => setSearch(detail.activeTabId === 'sessions' ? { tab: 'sessions' } : {})}
        tabs={[
          { id: 'audit', label: 'Audit trail', content: <AuditSection refreshKey={activityVersion} /> },
          { id: 'sessions', label: 'Sessions', content: <AgentSessions onStopped={() => setActivityVersion(value => value + 1)} /> },
        ]} />}
  </ContentLayout>;
}
