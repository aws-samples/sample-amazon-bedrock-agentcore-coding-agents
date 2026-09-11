import type { Terminal } from '@xterm/xterm';

export type TerminalOutput = (text: string, replay?: boolean) => void;

/**
 * History is a screen snapshot, not a conversation with the current PTY.
 * xterm answers device/cursor queries while parsing output. Replaying an old
 * TUI's queries into a shell sends those answers as unwanted shell input.
 *
 * Writes are asynchronous. Keep history and live frames in order, and suppress
 * input until the history write's parser callback completes. Live queries still
 * receive their normal replies. Each replay replaces the display, including
 * after an EventSource reconnect, rather than appending the history twice.
 */
export function createTerminalOutputQueue(terminal: Pick<Terminal, 'write' | 'options'>) {
  const pending: { text: string; replay: boolean }[] = [];
  let writing = false;
  let stopped = false;
  let restoreInput: (() => void) | undefined;

  function flush() {
    if (writing || stopped) return;
    const frame = pending.shift();
    if (!frame) return;
    writing = true;
    if (frame.replay) {
      const disabled = terminal.options.disableStdin;
      terminal.options.disableStdin = true;
      restoreInput = () => { terminal.options.disableStdin = disabled; };
    }
    terminal.write((frame.replay ? '\x1bc' : '') + frame.text, () => {
      restoreInput?.();
      restoreInput = undefined;
      writing = false;
      if (!stopped) flush();
    });
  }

  return {
    write(text: string, replay = false) {
      if (stopped || (!text && !replay)) return;
      pending.push({ text, replay });
      flush();
    },
    stop() {
      stopped = true;
      pending.length = 0;
      // An in-flight replay still has to finish with stdin disabled. Its
      // callback restores the option; the component normally disposes next.
    },
  };
}

export type TerminalOutputQueue = ReturnType<typeof createTerminalOutputQueue>;
