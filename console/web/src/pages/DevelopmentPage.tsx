import Header from '@cloudscape-design/components/header';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import { Workspace } from '../components/Workspace';
import { DEV_AGENT_ID } from './agents/environments';

export function DevelopmentPage() {
  return <div className="flex h-full min-h-0 flex-col">
    <div className="console-editor-header"><Header variant="h1"
      description="Edit files and run commands on the workshop host."
      actions={<StatusIndicator type="info">Workshop host</StatusIndicator>}>Development</Header></div>
    <div className="min-h-0 flex-1 overflow-hidden rounded-lg border border-border bg-card">
      <Workspace agentId={DEV_AGENT_ID} fullHeight />
    </div>
  </div>;
}
