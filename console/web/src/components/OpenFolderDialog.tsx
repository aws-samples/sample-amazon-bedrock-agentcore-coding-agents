import { useCallback, useEffect, useRef, useState } from 'react';
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter,
  Button, ScrollArea,
} from '@foxl/ui';
import { ChevronUp, Folder, FolderOpen, Home, HardDrive, Loader2 } from 'lucide-react';
import { listDirs, type ListDirsResult } from '../hooks/useSession';

/**
 * VS Code-style folder browser modal. Navigates the server's file system
 * (home-rooted, no upward escape) and commits the chosen directory via
 * `onConfirm(path)`. The browse starts at `initialPath` (default '~').
 */
export function OpenFolderDialog({
  open,
  onClose,
  sessionId,
  onConfirm,
  initialPath = '~',
}: {
  open: boolean;
  onClose: () => void;
  sessionId: string | null;
  onConfirm: (path: string) => void;
  initialPath?: string;
}) {
  const [result, setResult] = useState<ListDirsResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Track which path was last requested to avoid racing overlapping fetches.
  const pendingPath = useRef<string | null>(null);

  const navigate = useCallback(async (path: string) => {
    if (!sessionId) return;
    pendingPath.current = path;
    setLoading(true);
    setError(null);
    try {
      const r = await listDirs(sessionId, path);
      // Drop stale responses from a superseded navigation.
      if (pendingPath.current !== path) return;
      if (r.error) {
        setError(r.error);
        setResult(null);
      } else {
        setResult(r);
      }
    } catch (e) {
      if (pendingPath.current !== path) return;
      setError(String(e));
      setResult(null);
    } finally {
      if (pendingPath.current === path) setLoading(false);
    }
  }, [sessionId]);

  // Seed the browser each time the dialog opens.
  useEffect(() => {
    if (open && sessionId) {
      setResult(null);
      setError(null);
      navigate(initialPath);
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, sessionId]);

  function handleConfirm() {
    if (!result) return;
    onConfirm(result.path);
    onClose();
  }

  const currentPath = result?.path ?? initialPath;
  const canGoUp = !!result?.parent;

  return (
    <Dialog open={open} onOpenChange={(o) => { if (!o) onClose(); }}>
      <DialogContent
        slideFrom="center"
        overlayClassName="bg-background/60 backdrop-blur-sm"
        className="flex max-w-md flex-col gap-0 p-0 sm:max-w-lg"
      >
        {/* Title row */}
        <DialogHeader className="border-b border-border px-4 py-3 text-left">
          <DialogTitle className="text-[14px]">Open Folder</DialogTitle>
        </DialogHeader>

        {/* Location bar */}
        <div className="flex items-center gap-2 border-b border-border bg-muted/30 px-3 py-2">
          {/* Up button */}
          <button
            onClick={() => result?.parent && navigate(result.parent)}
            disabled={!canGoUp || loading}
            title="Go up one level"
            aria-label="Go up one level"
            className="rounded p-1 text-muted-foreground transition-colors hover:bg-accent hover:text-foreground disabled:cursor-not-allowed disabled:opacity-40"
          >
            <ChevronUp className="size-4" />
          </button>

          {/* Current path label */}
          <div className="min-w-0 flex-1">
            {loading ? (
              <span className="flex items-center gap-1.5 font-mono text-[12px] text-muted-foreground">
                <Loader2 className="size-3 animate-spin" />
                Loading…
              </span>
            ) : (
              <span
                className="block truncate font-mono text-[12px] text-foreground/80"
                title={currentPath}
              >
                {result?.label ?? currentPath}
              </span>
            )}
          </div>

          {/* Quick-access shortcuts */}
          <div className="flex shrink-0 items-center gap-1">
            <button
              onClick={() => navigate('~')}
              disabled={loading}
              title="Home (~)"
              aria-label="Navigate to home directory"
              className="flex items-center gap-1 rounded px-1.5 py-1 text-[11px] text-muted-foreground transition-colors hover:bg-accent hover:text-foreground disabled:opacity-40"
            >
              <Home className="size-3" />
              <span>Home</span>
            </button>
            <button
              onClick={() => navigate('/mnt/s3files')}
              disabled={loading}
              title="S3 Files (/mnt/s3files)"
              aria-label="Navigate to S3 Files mount"
              className="flex items-center gap-1 rounded px-1.5 py-1 text-[11px] text-muted-foreground transition-colors hover:bg-accent hover:text-foreground disabled:opacity-40"
            >
              <HardDrive className="size-3" />
              <span>S3 Files</span>
            </button>
          </div>
        </div>

        {/* Directory listing */}
        <ScrollArea className="h-64">
          <div className="py-1">
            {error ? (
              <div className="px-4 py-3 text-[12px] text-destructive">{error}</div>
            ) : loading ? (
              <div className="flex items-center justify-center py-8 text-[12px] text-muted-foreground">
                <Loader2 className="mr-2 size-4 animate-spin" />
                Loading directories…
              </div>
            ) : result && result.entries.length === 0 ? (
              <div className="px-4 py-6 text-center text-[12px] text-muted-foreground">
                No subdirectories
              </div>
            ) : result ? (
              result.entries.map((entry) => (
                <button
                  key={entry.path}
                  onClick={() => navigate(entry.path)}
                  className="flex w-full items-center gap-2.5 px-4 py-1.5 text-left text-[13px] text-foreground/80 transition-colors hover:bg-accent hover:text-foreground focus-visible:bg-accent focus-visible:outline-none"
                >
                  <Folder className="size-4 shrink-0 text-muted-foreground/70" />
                  <span className="truncate">{entry.name}</span>
                </button>
              ))
            ) : null}
          </div>
        </ScrollArea>

        {/* Footer: selected path + action buttons */}
        <div className="border-t border-border bg-muted/20 px-4 py-3">
          {/* Selected path preview */}
          <div className="mb-3 flex items-center gap-2 rounded-md border border-border bg-background px-2.5 py-1.5">
            <FolderOpen className="size-3.5 shrink-0 text-muted-foreground" />
            <span
              className="min-w-0 truncate font-mono text-[12px] text-foreground/70"
              title={currentPath}
            >
              {result?.label ?? currentPath}
            </span>
          </div>
          <DialogFooter className="flex-row justify-end gap-2">
            <Button variant="outline" size="sm" onClick={onClose}>
              Cancel
            </Button>
            <Button
              size="sm"
              onClick={handleConfirm}
              disabled={!result || loading}
            >
              Open this folder
            </Button>
          </DialogFooter>
        </div>
      </DialogContent>
    </Dialog>
  );
}
