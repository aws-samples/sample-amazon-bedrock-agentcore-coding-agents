import { useEffect, useImperativeHandle, useRef, forwardRef } from 'react';
// (refs below keep the data/resize callbacks fresh so xterm's one-time
//  onData/onResize binding never calls a stale closure.)
import { Terminal as Xterm } from 'xterm';
import { FitAddon } from '@xterm/addon-fit';
import 'xterm/css/xterm.css';

export interface TerminalHandle {
  write: (s: string) => void;
  fit: () => { rows: number; cols: number };
  focus: () => void;
  size: () => { rows: number; cols: number };
}

interface TerminalProps {
  onData?: (data: string) => void;
  onResize?: (size: { rows: number; cols: number }) => void;
  /** Hide the blinking cursor until the PTY is actually connected. */
  connected?: boolean;
}

/**
 * Thin xterm wrapper. The PTY is opened by the caller AFTER fit() so the shell
 * lays out at the size this pane actually renders (open -> fit -> size -> PTY).
 */
export const Terminal = forwardRef<TerminalHandle, TerminalProps>(function Terminal(
  { onData, onResize, connected = false },
  ref,
) {
  const hostRef = useRef<HTMLDivElement>(null);
  const term = useRef<Xterm | null>(null);
  const fit = useRef<FitAddon | null>(null);
  // Keep the latest callbacks in refs: xterm binds onData/onResize once, so a
  // direct closure would capture the first render's (null-session) handlers and
  // keystrokes would never be sent.
  const onDataRef = useRef(onData);
  const onResizeRef = useRef(onResize);
  onDataRef.current = onData;
  onResizeRef.current = onResize;

  // Fit to the pane, then shave ONE column so the rightmost cell never spills
  // past the pane edge (xterm's fit rounds up and can over-claim by a column,
  // which shows as a sliver of horizontal overflow). Resize the terminal to the
  // conservative width so the rendered grid and the PTY winsize always agree.
  const fitConservative = useRef(() => {
    const t = term.current;
    // A tab mounted hidden (display:none) has a 0x0 host: FitAddon would compute a
    // garbage 0-col grid and we'd push that wrong winsize to the PTY before the
    // pane is ever shown (R12). Skip while unmeasurable; the ResizeObserver fires
    // the moment the pane is revealed and fits at the real size then.
    const host = hostRef.current;
    if (!t || !host || host.offsetWidth === 0 || host.offsetHeight === 0) {
      return { rows: t?.rows ?? 24, cols: t?.cols ?? 80 };
    }
    fit.current?.fit();
    const rows = t.rows;
    const cols = Math.max(20, t.cols - 1);
    if (cols !== t.cols) t.resize(cols, rows);
    return { rows, cols };
  });

  useImperativeHandle(ref, () => ({
    write: (s) => term.current?.write(s),
    fit: () => fitConservative.current(),
    focus: () => term.current?.focus(),
    size: () => ({ rows: term.current?.rows ?? 24, cols: term.current?.cols ?? 80 }),
  }), []);

  useEffect(() => {
    if (!hostRef.current) return;
    const x = new Xterm({
      fontFamily: '"JetBrains Mono", ui-monospace, "SF Mono", "Cascadia Code", Menlo, monospace',
      fontSize: 12.5,
      lineHeight: 1.4,
      // Cursor starts hidden (matches background) so an unconnected pane shows
      // no stray block; it turns on once the PTY is live (effect below).
      theme: {
        background: '#1e1e1e',
        foreground: '#d4d4d4',
        cursor: '#1e1e1e',
        selectionBackground: '#264f78',
        brightBlack: '#666',
      },
      cursorBlink: false,
      convertEol: true,
      fontWeight: 400,
      fontWeightBold: 600,
    });
    const f = new FitAddon();
    x.loadAddon(f);
    x.open(hostRef.current);
    term.current = x;
    fit.current = f;
    fitConservative.current();   // initial fit at the conservative width
    x.onData((d) => onDataRef.current?.(d));
    x.onResize((s) => onResizeRef.current?.(s));

    // The synchronous fit above runs before the pane has its final layout width
    // and before the web font's metrics settle, so it under-counts columns and
    // the terminal opens narrower than the pane (a resize later corrects it).
    // Re-fit after two animation frames (layout flushed) and again once the font
    // is ready, so the FIRST winsize the caller reads/pushes is the real width.
    let raf1 = 0, raf2 = 0;
    raf1 = requestAnimationFrame(() => {
      raf2 = requestAnimationFrame(() => { try { fitConservative.current(); } catch { /* hidden */ } });
    });
    const fonts = (document as Document & { fonts?: FontFaceSet }).fonts;
    fonts?.ready?.then(() => { try { fitConservative.current(); } catch { /* hidden */ } });

    const ro = new ResizeObserver(() => {
      try { fitConservative.current(); } catch { /* pane hidden */ }
    });
    ro.observe(hostRef.current);
    return () => {
      cancelAnimationFrame(raf1); cancelAnimationFrame(raf2);
      ro.disconnect(); x.dispose(); term.current = null; fit.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Reveal the cursor only once the PTY is connected.
  useEffect(() => {
    const x = term.current;
    if (!x) return;
    x.options.cursorBlink = connected;
    x.options.theme = { ...x.options.theme, cursor: connected ? '#d4d4d4' : '#1e1e1e' };
  }, [connected]);

  return <div ref={hostRef} className="h-full w-full overflow-hidden bg-[#1e1e1e] px-[10px] py-2" />;
});
