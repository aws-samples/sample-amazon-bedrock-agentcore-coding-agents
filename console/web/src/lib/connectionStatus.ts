/** Absence of a successful read is not evidence of absent configuration. */
export function connectionStatus(
  loading: boolean,
  expired: boolean,
  connection: { connected: boolean | null; error?: string } | null,
  readFailed = false,
): { type: 'loading' | 'error' | 'success' | 'pending'; label: string } {
  if (expired) return { type: 'error', label: 'Session expired' };
  if (loading) return { type: 'loading', label: 'Loading' };
  if (readFailed || !connection || connection.connected === null || connection.error) return { type: 'error', label: 'Unable to verify' };
  return connection.connected ? { type: 'success', label: 'Configured' } : { type: 'pending', label: 'Not configured' };
}
