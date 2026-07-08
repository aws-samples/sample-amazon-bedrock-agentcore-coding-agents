import { Suspense, lazy } from 'react';
import { Navigate, Route, Routes, useLocation, useNavigate } from 'react-router-dom';
import { SquareTerminal, Boxes, ShieldCheck, Bot, Settings, PanelLeftClose, PanelLeft } from 'lucide-react';
import {
  AppShell, NavSidebar, type NavSidebarGroup, Toaster, useSidebar,
  SidebarGroup, SidebarGroupContent, SidebarMenu, SidebarMenuItem, SidebarMenuButton,
} from '@foxl/ui';
import { Brand } from './components/Brand';
import { AgentsSubNav } from './components/AgentsSubNav';
import { ChatList } from './components/ChatList';
import { GovernanceSubNav } from './components/GovernanceSubNav';

// Routes are code-split: each page is its own chunk loaded on navigation, so
// the landing route (/agents) no longer ships the chat's markdown + syntax
// highlighter (FleetsPage) or the metrics page up front. First paint pulls only
// the shell + the page you actually land on.
const DevelopmentPage = lazy(() => import('./pages/DevelopmentPage').then((m) => ({ default: m.DevelopmentPage })));
const AgentsPage = lazy(() => import('./pages/AgentsPage').then((m) => ({ default: m.AgentsPage })));
const FleetsPage = lazy(() => import('./pages/FleetsPage').then((m) => ({ default: m.FleetsPage })));
const GovernancePage = lazy(() => import('./pages/GovernancePage').then((m) => ({ default: m.GovernancePage })));
const SettingsPage = lazy(() => import('./pages/SettingsPage').then((m) => ({ default: m.SettingsPage })));

// Lightweight route fallback: a calm centered pulse, not a layout-shifting
// spinner. Honors reduced-motion via the shared utility.
function RouteFallback() {
  return (
    <div className="flex h-full items-center justify-center">
      <span className="size-2 animate-pulse rounded-full bg-muted-foreground/40" />
    </div>
  );
}

const NAV = [
  { id: 'development', label: 'Development', sub: 'Build & deploy in a live shell', icon: SquareTerminal, path: '/development' },
  { id: 'agents', label: 'Agents', sub: 'Wire & deploy the coding agents', icon: Bot, path: '/agents' },
  { id: 'fleets', label: 'Chat', sub: 'Talk to the orchestrator; it runs the fleet', icon: Boxes, path: '/fleets' },
  { id: 'governance', label: 'Governance', sub: 'Cost, identity & audit', icon: ShieldCheck, path: '/governance' },
];

function Shell({ children }: { children: React.ReactNode }) {
  const nav = useNavigate();
  const { pathname } = useLocation();
  // Two independent axes, do NOT conflate them:
  //   - contained: whether the shell wraps children in its max-w-6xl centering
  //     container. Every page here centers its OWN content (or is a full-bleed
  //     workspace), so this stays false for all -- adding the wrapper would
  //     double-center / narrow them.
  //   - scroll: whether <main> owns the vertical scroll. Every page scrolls as a
  //     whole EXCEPT Chat (/fleets), which pins a top bar + bottom composer and
  //     manages its own inner scroll region; main scrolling there would drag the
  //     pinned chrome. Before scroll isolation main was always overflow-y-auto,
  //     so anything other than Chat losing its scroll is a regression.
  const scroll = !pathname.startsWith('/fleets');
  const groups: NavSidebarGroup[] = [
    {
      items: NAV.map((n) => ({
        id: n.id,
        label: n.label,
        icon: n.icon,
        isActive: pathname.startsWith(n.path),
        onSelect: () => nav(n.path),
        // Inline sub-lists under a nav item: the Module 1 workspaces under
        // "Agents" (deep-linkable at /agents/<env>), the run history under
        // "Tasks" (deep-linkable at /fleets/<id>), and the governance sections
        // under "Governance" (deep-linkable at /governance/<section>).
        after:
          n.id === 'agents' ? <AgentsSubNav />
          : n.id === 'fleets' ? <ChatList />
          : n.id === 'governance' ? <GovernanceSubNav />
          : undefined,
      })),
    },
  ];
  return (
    <AppShell
      contained={false}
      scroll={scroll}
      sidebar={
        <NavSidebar
          header={<SidebarHeaderContent />}
          groups={groups}
          footer={<SidebarFooterContent active={pathname.startsWith('/settings')} onSettings={() => nav('/settings')} />}
        />
      }
    >
      {/* Suspense lives INSIDE the shell so the sidebar paints instantly and
          only the page area shows the fallback while its chunk loads. */}
      <Suspense fallback={<RouteFallback />}>{children}</Suspense>
    </AppShell>
  );
}


function SidebarFooterContent({ active, onSettings }: { active: boolean; onSettings: () => void }) {
  // Mirror the nav items' EXACT wrapper chain (SidebarGroup p-2 ->
  // SidebarGroupContent -> SidebarMenu -> SidebarMenuButton) so Settings sits in
  // the same icon column as Development/Agents/Tasks/Governance, collapsed or not.
  // Wrapping in only a bare SidebarMenu drops the SidebarGroup's p-2 and the icon
  // shifts left of the nav icons when collapsed.
  return (
    <SidebarGroup className="py-1">
      <SidebarGroupContent>
        <SidebarMenu>
          <SidebarMenuItem>
            <SidebarMenuButton isActive={active} onClick={onSettings} tooltip="Settings" className="w-full">
              <Settings className="h-4 w-4" />
              <span>Settings</span>
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarGroupContent>
    </SidebarGroup>
  );
}

function SidebarHeaderContent() {
  const { state, toggleSidebar } = useSidebar();
  const collapsed = state === 'collapsed';
  // Collapsed: the toggle is a size-8 square left-aligned, matching the nav
  // SidebarMenuButton icon box so it sits in the same column. Expanded: brand on
  // the left, toggle on the right.
  return (
    <div className={collapsed ? 'flex items-center' : 'flex items-center justify-between gap-2'}>
      {!collapsed && <Brand />}
      <button
        onClick={toggleSidebar}
        title={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
        className="flex size-8 items-center justify-center rounded-md text-muted-foreground hover:bg-accent hover:text-accent-foreground"
      >
        {collapsed ? <PanelLeft className="size-4" /> : <PanelLeftClose className="size-4" />}
      </button>
    </div>
  );
}

export default function App() {
  return (
    <>
      <Routes>
        <Route path="/" element={<Navigate to="/development" replace />} />
        <Route path="/development" element={<Shell><DevelopmentPage /></Shell>} />
        <Route path="/agents" element={<Shell><AgentsPage /></Shell>} />
        <Route path="/agents/:env" element={<Shell><AgentsPage /></Shell>} />
        <Route path="/fleets" element={<Shell><FleetsPage /></Shell>} />
        {/* /fleets/c/:chatId selects a sub-chat (conversation); /fleets/:runId
            deep-links a run. The /c/ prefix keeps the two namespaces distinct. */}
        <Route path="/fleets/c/:chatId" element={<Shell><FleetsPage /></Shell>} />
        <Route path="/fleets/:runId" element={<Shell><FleetsPage /></Shell>} />
        <Route path="/governance" element={<Shell><GovernancePage /></Shell>} />
        <Route path="/governance/:section" element={<Shell><GovernancePage /></Shell>} />
        <Route path="/settings" element={<Shell><SettingsPage /></Shell>} />
        <Route path="*" element={<Navigate to="/development" replace />} />
      </Routes>
      <Toaster richColors closeButton position="bottom-right" />
    </>
  );
}
