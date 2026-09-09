import { Suspense, lazy, useEffect, useRef, useState } from 'react';
import { Navigate, Outlet, Route, Routes, useHref, useLocation, useNavigate, useParams } from 'react-router-dom';
import { useTheme } from 'next-themes';
import AppLayout, { type AppLayoutProps } from '@cloudscape-design/components/app-layout';
import Autosuggest from '@cloudscape-design/components/autosuggest';
import BreadcrumbGroup from '@cloudscape-design/components/breadcrumb-group';
import Button from '@cloudscape-design/components/button';
import FormField from '@cloudscape-design/components/form-field';
import HelpPanel from '@cloudscape-design/components/help-panel';
import Link from '@cloudscape-design/components/link';
import Modal from '@cloudscape-design/components/modal';
import RadioGroup from '@cloudscape-design/components/radio-group';
import SideNavigation from '@cloudscape-design/components/side-navigation';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Spinner from '@cloudscape-design/components/spinner';
import TextContent from '@cloudscape-design/components/text-content';
import TopNavigation from '@cloudscape-design/components/top-navigation';
import { applyDensity, applyMode, Density, Mode } from '@cloudscape-design/global-styles';
import { getAuthMe, getAttributionConfiguration, type AuthUser } from './api';
import { ConsoleNotifications } from './components/ConsoleNotifications';

const DevelopmentPage = lazy(() => import('./pages/DevelopmentPage').then(m => ({ default: m.DevelopmentPage })));
const AgentsPage = lazy(() => import('./pages/AgentsPage').then(m => ({ default: m.AgentsPage })));
const ChatPage = lazy(() => import('./pages/ChatPage').then(m => ({ default: m.ChatPage })));
const GovernancePage = lazy(() => import('./pages/GovernancePage').then(m => ({ default: m.GovernancePage })));
const SettingsPage = lazy(() => import('./pages/SettingsPage').then(m => ({ default: m.SettingsPage })));

const PAGES = [
  { path: '/development', title: 'Development', description: 'Edit files and run commands on the workshop host. Runtime agent sessions are available under Agents.' },
  { path: '/agents', title: 'Agents', description: 'Switch between your coding agents, open their live terminals, and manage sessions in one place. Opening a session starts the agent.' },
  { path: '/chat', title: 'Chat', description: 'Submit a goal to the coordinator and inspect each pull request’s executable check and independent review. Approval and merge are separate decisions.' },
  { path: '/governance/usage', title: 'Usage', description: 'Query exported CloudWatch request events, inspect user attribution, and save the evidence.' },
  { path: '/governance/controls', title: 'Controls', description: 'Inspect submitter identity, pull request approvals, and execution limits. Evaluate an action with the coordinator policy checker.' },
  { path: '/governance/activity', title: 'Activity', description: 'Find recorded operations in the host audit trail, inspect Runtime sessions, and stop work you have finished.' },
  { path: '/settings', title: 'Settings', description: 'Configure the GitHub destination, merge policy, Kiro credential, and Runtime connections used by this console.' },
];

function Shell() {
  const { pathname, search } = useLocation();
  const navigate = useNavigate();
  const base = useHref('/').replace(/\/$/, '');
  const href = (path: string) => `${base}${path}`;
  const layout = useRef<AppLayoutProps.Ref>(null);
  const [toolsOpen, setToolsOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [region, setRegion] = useState<string | null>(null);
  const [user, setUser] = useState<AuthUser | null>(null);
  const [preferencesOpen, setPreferencesOpen] = useState(false);
  const { resolvedTheme, setTheme } = useTheme();
  const [density, setDensity] = useState(() => {
    try { return localStorage.getItem('agentcore.console.density') === 'compact' ? 'compact' : 'comfortable'; }
    catch { return 'comfortable'; }
  });
  const [draftTheme, setDraftTheme] = useState('light');
  const [draftDensity, setDraftDensity] = useState(density);
  const page = PAGES.find(p => pathname === p.path)
    ?? PAGES.find(p => pathname.startsWith(`${p.path}/`))
    ?? PAGES[0]!;
  const workspace = pathname.startsWith('/development') || pathname.startsWith('/chat');
  const currentHref = href(page.path);
  const activeSection = page.path;

  useEffect(() => {
    let live = true;
    getAttributionConfiguration().then(s => { if (live) setRegion(s.region || null); }).catch(() => {});
    getAuthMe().then(s => { if (live) setUser(s); });
    return () => { live = false; };
  }, []);
  useEffect(() => { applyMode(resolvedTheme === 'dark' ? Mode.Dark : Mode.Light); }, [resolvedTheme]);
  useEffect(() => {
    applyDensity(density === 'compact' ? Density.Compact : Density.Comfortable);
    try { localStorage.setItem('agentcore.console.density', density); } catch { /* Optional storage. */ }
  }, [density]);
  useEffect(() => { document.title = `${page.title} | Agent Studio`; }, [page.title]);

  function follow(target: string) {
    navigate(base && target.startsWith(base) ? target.slice(base.length) || '/' : target);
    layout.current?.closeNavigationIfNecessary();
  }
  function openPreferences() {
    setDraftTheme(resolvedTheme === 'dark' ? 'dark' : 'light');
    setDraftDensity(density);
    setPreferencesOpen(true);
  }
  const breadcrumbs = [
    { text: 'Agent Studio', href: href('/development') },
    ...(pathname.startsWith('/governance/') ? [{ text: 'Governance', href: href('/governance/usage') }] : []),
    { text: page.title, href: currentHref },
  ];
  if (pathname.startsWith('/chat/')) {
    breadcrumbs.push({ text: pathname.startsWith('/chat/c/') ? 'Conversation' : 'Build details', href: href(pathname) });
  } else if (pathname === '/agents' && new URLSearchParams(search).get('view') === 'sessions') {
    breadcrumbs.push({ text: 'Sessions', href: `${href(pathname)}${search}` });
  }
  return (
    <>
      <a className="skip-link" href={`${href(pathname)}${search}#console-main`}>Skip to main content</a>
      <div id="console-top-navigation">
        <TopNavigation
          visualContext="top-navigation"
          identity={{ title: 'Agent Studio', href: href('/development'),
            onFollow: e => { e.preventDefault(); follow(href('/development')); } }}
          search={<Autosuggest
            ariaLabel="Search pages in this console" placeholder="Search this console"
            value={query} onChange={({ detail }) => setQuery(detail.value)}
            options={PAGES.map(p => ({ value: p.path, label: p.title, description: p.description }))}
            filteringType="auto" hideEnteredTextOption empty="No matching pages"
            enteredTextLabel={value => `Page search: ${value}`}
            onSelect={({ detail }) => {
              if (detail.selectedOption?.value) {
                navigate(detail.selectedOption.value); setQuery(''); layout.current?.closeNavigationIfNecessary();
              }
            }}
          />}
          utilities={[
            { type: 'menu-dropdown', text: region || 'Region', title: 'Deployment region',
              description: 'This workshop uses the region configured on its host.',
              items: [{ id: 'region', text: region || 'No region returned by the host', disabled: true }] },
            { type: 'button', text: 'Help', iconName: 'status-info', ariaLabel: 'Open contextual help',
              onClick: () => setToolsOpen(v => !v) },
            { type: 'button', text: 'Settings', iconName: 'settings', href: href('/settings'),
              onFollow: e => { e.preventDefault(); follow(href('/settings')); } },
            { type: 'menu-dropdown', text: user?.authenticated ? user.email || user.name || 'Signed in' : 'Local session',
              iconName: 'user-profile', description: user?.authenticated ? 'Workshop console identity' : 'No Cognito session on this host',
              items: [{ id: 'preferences', text: 'Preferences' },
                ...(user?.authenticated ? [{ id: 'sign-out', text: 'Sign out', href: '/auth/logout' }] : [])],
              onItemClick: ({ detail }) => { if (detail.id === 'preferences') openPreferences(); } },
          ]}
          i18nStrings={{ searchIconAriaLabel: 'Search this console', searchDismissIconAriaLabel: 'Close search',
            overflowMenuTriggerText: 'More', overflowMenuTitleText: 'Console utilities',
            overflowMenuBackIconAriaLabel: 'Back', overflowMenuDismissIconAriaLabel: 'Close utilities' }}
        />
      </div>
      <AppLayout
        ref={layout} headerSelector="#console-top-navigation" footerSelector="#console-footer"
        navigationWidth={280} maxContentWidth={pathname === '/settings' ? 1280 : pathname.startsWith('/governance') ? 1440 : Number.MAX_VALUE}
        contentType={pathname === '/settings' ? 'form' : 'default'}
        navigation={<SideNavigation
          header={{ text: 'Agent Studio', href: href('/development') }}
          activeHref={href(activeSection)}
          onFollow={e => { if (!e.detail.external) { e.preventDefault(); follow(e.detail.href); } }}
          items={[
            { type: 'section', text: 'Workspace', items: [
              { type: 'link', text: 'Development', href: href('/development') },
              { type: 'link', text: 'Agents', href: href('/agents') },
              { type: 'link', text: 'Chat', href: href('/chat') },
            ] },
            { type: 'divider' },
            { type: 'section', text: 'Governance', items: [
              { type: 'link', text: 'Usage', href: href('/governance/usage') },
              { type: 'link', text: 'Controls', href: href('/governance/controls') },
              { type: 'link', text: 'Activity', href: href('/governance/activity') },
            ] },
          ]}
        />}
        breadcrumbs={<BreadcrumbGroup ariaLabel="Breadcrumbs" items={breadcrumbs}
          onFollow={e => { e.preventDefault(); follow(e.detail.href); }} />}
        notifications={<ConsoleNotifications />}
        toolsOpen={toolsOpen} onToolsChange={({ detail }) => setToolsOpen(detail.open)}
        tools={<HelpPanel header={<h2>{page.title}</h2>}
          footer={<TextContent><h3>Learn more</h3><ul>
            <li><Link external href="https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/">AgentCore documentation</Link></li>
            <li><Link external href="https://github.com/aws-samples/sample-amazon-bedrock-agentcore-coding-agents">Workshop source</Link></li>
          </ul></TextContent>}>
          <TextContent>
            <p>{page.description}</p><h3>Execution context</h3>
            <p>Requests submitted in Chat use the coordinator on this host. Follow a build submitted to the deployed coordinator with its CLI watcher.</p>
            <h3>Before you start</h3>
            <p>Use Settings to review the GitHub repository and Runtime connections. Each builder creates a pull request with its own check and review evidence.</p>
          </TextContent>
        </HelpPanel>}
        ariaLabels={{ navigation: 'Service navigation', navigationClose: 'Close navigation', navigationToggle: 'Open navigation',
          tools: 'Contextual help', toolsClose: 'Close help', toolsToggle: 'Open help', notifications: 'Notifications' }}
        content={<div id="console-main" tabIndex={-1} className={workspace ? 'console-workspace' : 'console-content'}>
          <Suspense fallback={<div className="console-loading" role="status"><Spinner /> Loading page…</div>}><Outlet /></Suspense>
        </div>}
      />
      <footer id="console-footer"><span>Powered by Amazon Bedrock AgentCore</span><span>Workshop environment</span></footer>
      <Modal visible={preferencesOpen} header="Preferences" onDismiss={() => setPreferencesOpen(false)} closeAriaLabel="Close preferences"
        footer={<SpaceBetween direction="horizontal" size="xs">
          <Button variant="link" onClick={() => setPreferencesOpen(false)}>Cancel</Button>
          <Button variant="primary" onClick={() => { setTheme(draftTheme); setDensity(draftDensity); setPreferencesOpen(false); }}>Save preferences</Button>
        </SpaceBetween>}>
        <SpaceBetween size="l">
          <FormField label="Color mode"><RadioGroup value={draftTheme} onChange={({ detail }) => setDraftTheme(detail.value)}
            items={[{ value: 'light', label: 'Light' }, { value: 'dark', label: 'Dark' }]} /></FormField>
          <FormField label="Content density"><RadioGroup value={draftDensity} onChange={({ detail }) => setDraftDensity(detail.value)}
            items={[{ value: 'comfortable', label: 'Comfortable' }, { value: 'compact', label: 'Compact' }]} /></FormField>
        </SpaceBetween>
      </Modal>
    </>
  );
}
function LegacyChatRedirect() {
  const { pathname, search } = useLocation();
  return <Navigate to={`${pathname.replace(/^\/fleets/, '/chat')}${search}`} replace />;
}
function LegacyAgentRedirect() {
  const { env } = useParams();
  return <Navigate to={`/agents?agent=${encodeURIComponent(env || '')}`} replace />;
}
function GovernanceRoute() {
  const { section } = useParams();
  if (section === 'usage' || section === 'controls' || section === 'activity') return <GovernancePage section={section} />;
  return <Navigate to={section === 'sessions' ? '/governance/activity?tab=sessions'
    : section === 'runtimes' ? '/agents'
    : section === 'audit' ? '/governance/activity'
    : section === 'policies' || section === 'identity' ? '/governance/controls'
    : '/governance/usage'} replace />;
}
export default function App() {
  return <Routes><Route element={<Shell />}>
    <Route path="/" element={<Navigate to="/development" replace />} />
    <Route path="/development" element={<DevelopmentPage />} />
    <Route path="/agents" element={<AgentsPage />} /><Route path="/agents/:env" element={<LegacyAgentRedirect />} />
    <Route path="/chat" element={<ChatPage />} /><Route path="/chat/c/:chatId" element={<ChatPage />} />
    <Route path="/chat/:runId" element={<ChatPage />} />
    <Route path="/fleets/*" element={<LegacyChatRedirect />} />
    <Route path="/governance" element={<Navigate to="/governance/usage" replace />} />
    <Route path="/governance/:section" element={<GovernanceRoute />} /><Route path="/settings" element={<SettingsPage />} />
    <Route path="*" element={<Navigate to="/development" replace />} />
  </Route></Routes>;
}
