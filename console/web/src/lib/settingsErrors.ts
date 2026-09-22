import { SessionExpiredError } from './authSession.ts';

export type SettingsErrorOrigin = 'action' | 'read';
export interface SettingsError {
  message: string;
  origin: SettingsErrorOrigin;
  expiryRevision?: number;
}

export function settingsErrorMessage(error: SettingsError | undefined, revision: number): string {
  // A revision advances only when an explicit sign-in check restores access.
  if (!error || (error.expiryRevision !== undefined && revision > error.expiryRevision)) return '';
  return error.message;
}

export function recordSettingsError(
  previous: SettingsError | undefined, reason: unknown, revision: number,
  origin: SettingsErrorOrigin = 'action',
): SettingsError {
  // Refreshing saved settings cannot resolve a rejected edit or invalid draft.
  if (origin === 'read' && previous?.origin === 'action' && settingsErrorMessage(previous, revision)) return previous;
  return {
    message: typeof reason === 'string' ? reason : reason instanceof Error ? reason.message : 'The request failed. Try again.',
    origin,
    ...(reason instanceof SessionExpiredError ? { expiryRevision: revision } : {}),
  };
}
