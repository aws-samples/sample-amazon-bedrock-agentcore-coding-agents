/**
 * Preserve a terminal's byte order across HTTP requests. xterm can emit a paste,
 * individual keystrokes, and Enter before the first request has completed.
 * Each terminal owns a queue; a slow Runtime must not block another terminal.
 */
export function createTerminalInputQueue(
  transmit: (input: string) => Promise<unknown>,
  onError: (error: Error) => void,
) {
  let pending = '';
  let sending = false;
  let stopped = false;

  async function flush() {
    sending = true;
    try {
      while (pending && !stopped) {
        const input = pending;
        pending = '';
        await transmit(input);
      }
    } catch (cause) {
      // A failed response does not tell us whether the shell received the input.
      // Replaying a command could execute it twice; require an explicit reconnect.
      stopped = true;
      pending = '';
      onError(cause instanceof Error ? cause : new Error(String(cause)));
    } finally {
      sending = false;
    }
  }

  return {
    send(input: string) {
      if (stopped || !input) return;
      pending += input;
      if (!sending) void flush();
    },
    stop() {
      stopped = true;
      pending = '';
    },
  };
}

export type TerminalInputQueue = ReturnType<typeof createTerminalInputQueue>;
