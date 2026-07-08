import { useEffect, useRef, useState } from 'react';
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter,
  AlertDialog, AlertDialogContent, AlertDialogHeader, AlertDialogTitle, AlertDialogDescription,
  AlertDialogFooter, AlertDialogCancel, AlertDialogAction,
  Button, Input, Label,
} from '@foxl/ui';

/**
 * Workspace file-op dialogs, replacing window.prompt / alert / confirm (which
 * read as unstyled browser chrome). A single PromptDialog drives New file / New
 * folder / Rename (text input + inline error), and a destructive AlertDialog
 * confirms delete. Both are controlled by the Workspace via a small request
 * object so the explorer code stays declarative.
 */

export type PromptKind = 'new-file' | 'new-folder' | 'rename' | 'open-folder';

export interface PromptRequest {
  kind: PromptKind;
  /** Prefilled value (e.g. the current name when renaming). */
  initial: string;
  /** Resolve the entered name; return an error string to keep the dialog open. */
  onSubmit: (value: string) => Promise<string | null>;
}

const PROMPT_COPY: Record<PromptKind, { title: string; label: string; placeholder: string; cta: string }> = {
  'new-file': { title: 'New file', label: 'File name', placeholder: 'untitled.txt', cta: 'Create file' },
  'new-folder': { title: 'New folder', label: 'Folder name', placeholder: 'new-folder', cta: 'Create folder' },
  'rename': { title: 'Rename', label: 'New name', placeholder: '', cta: 'Rename' },
  'open-folder': { title: 'Open folder', label: 'Folder path', placeholder: '~/projects or /mnt/s3files', cta: 'Open folder' },
};

export function PromptDialog({ request, onClose }: { request: PromptRequest | null; onClose: () => void }) {
  const [value, setValue] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const boundTo = useRef<PromptRequest | null>(null);

  // Reset fields each time a new request opens the dialog.
  useEffect(() => {
    if (request && boundTo.current !== request) {
      boundTo.current = request;
      setValue(request.initial);
      setError('');
      setBusy(false);
    }
    if (!request) boundTo.current = null;
  }, [request]);

  if (!request) return null;
  const req = request;       // non-null capture for the async closure
  const copy = PROMPT_COPY[req.kind];

  async function submit() {
    const name = value.trim();
    if (!name) { setError('Name cannot be empty.'); return; }
    setBusy(true);
    setError('');
    const err = await req.onSubmit(name);
    if (err) { setError(err); setBusy(false); return; }
    onClose();
  }

  return (
    <Dialog open onOpenChange={(o) => { if (!o) onClose(); }}>
      <DialogContent slideFrom="center" overlayClassName="bg-background/60" className="max-w-sm">
        <DialogHeader className="text-left">
          <DialogTitle>{copy.title}</DialogTitle>
          <DialogDescription>
            {req.kind === 'open-folder'
              ? 'Set the workspace root. ~ expands to your home directory.'
              : 'Created in the current workspace folder.'}
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-1.5 py-1">
          <Label htmlFor="ws-name">{copy.label}</Label>
          <Input
            id="ws-name"
            value={value}
            autoFocus
            placeholder={copy.placeholder}
            disabled={busy}
            onChange={(e) => { setValue(e.target.value); setError(''); }}
            onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); submit(); } }}
          />
          {error && <p className="text-sm text-destructive">{error}</p>}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={busy}>Cancel</Button>
          <Button onClick={submit} disabled={busy}>{busy ? 'Working...' : copy.cta}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export interface ConfirmRequest {
  name: string;
  isDir: boolean;
  /** 'delete' (default) removes a file/folder; 'unsaved' closes a dirty tab. */
  kind?: 'delete' | 'unsaved';
  onConfirm: () => Promise<void>;
}

export function ConfirmDeleteDialog({ request, onClose }: { request: ConfirmRequest | null; onClose: () => void }) {
  const [busy, setBusy] = useState(false);
  if (!request) return null;
  const req = request;       // non-null capture for the async closure
  const unsaved = req.kind === 'unsaved';
  const what = req.isDir ? 'folder' : 'file';

  async function go() {
    setBusy(true);
    await req.onConfirm();
    setBusy(false);
    onClose();
  }

  return (
    <AlertDialog open onOpenChange={(o) => { if (!o) onClose(); }}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{unsaved ? 'Discard unsaved changes?' : `Delete ${what}?`}</AlertDialogTitle>
          <AlertDialogDescription>
            <span className="font-mono">{request.name}</span>{' '}
            {unsaved
              ? 'has unsaved changes. Close it and discard your edits?'
              : 'will be removed from the workspace. This cannot be undone.'}
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel disabled={busy}>Cancel</AlertDialogCancel>
          <AlertDialogAction
            onClick={(e) => { e.preventDefault(); go(); }}
            disabled={busy}
            className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
          >
            {busy ? 'Working...' : (unsaved ? 'Discard' : `Delete ${what}`)}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
