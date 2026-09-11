export interface TerminalSize {
  rows: number;
  cols: number;
}

/**
 * Keep a PTY's final dimensions in order across HTTP requests. Layout changes
 * may arrive faster than the Runtime responds; retain only the newest waiting
 * size, and never let a slow older request follow the final one.
 */
export function createTerminalResizeQueue(
  transmit: (size: TerminalSize) => Promise<unknown>,
  onError: (error: Error) => void,
) {
  let pending: TerminalSize | undefined;
  let applied: TerminalSize | undefined;
  let sending = false;
  let stopped = false;

  async function flush() {
    sending = true;
    try {
      while (pending && !stopped) {
        const size = pending;
        pending = undefined;
        if (applied?.cols === size.cols && applied.rows === size.rows) continue;
        try {
          await transmit(size);
          applied = size;
        } catch (cause) {
          applied = undefined;
          onError(cause instanceof Error ? cause : new Error(String(cause)));
        }
      }
    } finally {
      sending = false;
    }
  }

  return {
    resize(size: TerminalSize) {
      if (stopped) return;
      pending = { rows: size.rows, cols: size.cols };
      if (!sending) void flush();
    },
    stop() {
      stopped = true;
      pending = undefined;
    },
  };
}

export type TerminalResizeQueue = ReturnType<typeof createTerminalResizeQueue>;
