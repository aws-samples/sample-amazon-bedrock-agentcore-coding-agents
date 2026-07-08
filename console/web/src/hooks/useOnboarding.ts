import { useCallback, useEffect, useState } from 'react';

/**
 * Onboarding modal state for the console GUIDE. The modal is a static welcome
 * map of the four sidebar areas, not a readiness tracker, so this hook holds no
 * backend status: just whether the modal is open, plus the persisted "dismissed"
 * bit. It auto-opens once on a fresh console and is reopenable from the sidebar
 * "Setup guide" button (via the window-event bridge below).
 */

export interface OnboardingState {
  /** The onboarding modal is open. */
  open: boolean;
  /** Open the modal (the sidebar "Setup guide" affordance). */
  show: () => void;
  /** Close the modal and remember the dismissal (persisted). */
  dismiss: () => void;
}

const DISMISS_KEY = 'agentcore.console.onboarding.dismissed';

function readDismissed(): boolean {
  try {
    return localStorage.getItem(DISMISS_KEY) === '1';
  } catch {
    // Private mode / disabled storage: treat as not-dismissed (show once).
    return false;
  }
}

function writeDismissed(value: boolean): void {
  try {
    if (value) localStorage.setItem(DISMISS_KEY, '1');
    else localStorage.removeItem(DISMISS_KEY);
  } catch {
    // Persisting failed (private mode): state still changes for this session.
  }
}

// Reopen bridge: the modal mounts once (globally) and owns its open state, but
// the sidebar "Setup guide" button lives elsewhere in the tree. A window event
// lets that button reopen the modal without a shared store or a context refactor.
const OPEN_EVENT = 'agentcore:onboarding:open';
export function openOnboarding(): void {
  window.dispatchEvent(new Event(OPEN_EVENT));
}

export function useOnboarding(): OnboardingState {
  const [open, setOpen] = useState(false);

  // First visit: auto-open once on a fresh console (not previously dismissed).
  useEffect(() => {
    if (!readDismissed()) setOpen(true);
  }, []);

  // Reopen on the window event the sidebar "Setup guide" button dispatches.
  useEffect(() => {
    const onOpen = () => setOpen(true);
    window.addEventListener(OPEN_EVENT, onOpen);
    return () => window.removeEventListener(OPEN_EVENT, onOpen);
  }, []);

  const show = useCallback(() => setOpen(true), []);
  const dismiss = useCallback(() => {
    writeDismissed(true);
    setOpen(false);
  }, []);

  return { open, show, dismiss };
}
