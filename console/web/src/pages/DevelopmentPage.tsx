import { SquareTerminal } from 'lucide-react';
import { Workspace } from '../components/Workspace';
import { DEV_AGENT_ID } from './agents/environments';

/**
 * Development: the main Module 1 workspace, an IDE-style full-height shell (file
 * tree + editor + terminal) that fills the viewport, distinct from the small
 * role-config cards on the Agents page. A live shell on the workshop box starts
 * at ~/src (the cloned repo); the attendee opens the shared /mnt/s3files mount
 * with Open Folder after creating S3 Files in Stage 1.
 * This is where you write code, run the agentcore CLI, build images, and deploy.
 * The three coding agents live under Agents; their deploys land on the Fleet.
 */
export function DevelopmentPage() {
  return (
    // Fill the main content region; no max-width container, no page padding, so
    // the IDE reads edge-to-edge like an editor, not a card on a marketing page.
    // Bind to the viewport height (`h-svh` + `overflow-hidden`): the app shell's
    // <main> is `flex-1` inside a `min-h-svh` column, so it grows to content unless
    // a child caps it. Without this cap a long file tree makes the WHOLE main
    // scroll, so the sidebar and editor move together; capping at the viewport
    // makes each inner pane (tree / editor / terminal) own its OWN scroll instead.
    <div className="flex h-svh min-h-0 flex-col overflow-hidden">
      {/* Slim IDE title bar. The Explorer row below shows the live folder root. */}
      <div className="flex items-center gap-2.5 border-b border-border bg-sidebar/40 px-4 py-2.5">
        <SquareTerminal className="size-4 text-muted-foreground" />
        <span className="text-sm font-medium">Development</span>
        <span className="ml-auto hidden font-mono text-[11px] text-muted-foreground sm:inline">
          box workspace
        </span>
      </div>
      {/* The IDE itself fills the rest of the height. */}
      <div className="min-h-0 flex-1">
        <Workspace agentId={DEV_AGENT_ID} fullHeight />
      </div>
    </div>
  );
}
