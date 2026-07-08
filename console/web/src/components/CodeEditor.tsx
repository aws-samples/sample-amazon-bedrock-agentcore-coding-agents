import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { X, ArrowDown, ArrowUp, Replace } from 'lucide-react';

/**
 * A plain-text code editor with a VS Code-style line-number gutter and the basic
 * editing UX an editor has that a bare <textarea> lacks. The body is a real
 * <textarea> (kept class `resize-none` so the workshop styling and the capture
 * harness selector both match); a sibling gutter renders one right-aligned number
 * per logical line, mirrors the textarea scroll, and emphasizes the caret's line.
 *
 * Soft-wrap is OFF (`wrap="off"`, `whiteSpace: pre`): one visual row per logical
 * line, so a long line scrolls horizontally instead of desyncing the gutter.
 *
 * Keymap (VS Code parity):
 *   Tab / Shift+Tab        indent / dedent (selection-aware)
 *   Enter                  auto-indent (carry leading whitespace; +1 level after :/{/[/()
 *   ( [ { " ' `            auto-close, and wrap a selection
 *   Backspace              delete an empty bracket pair together
 *   Cmd/Ctrl + /           toggle line comment (language-aware)
 *   Alt + Up/Down          move the current line(s)
 *   Shift+Alt + Down/Up    duplicate the current line(s)
 *   Cmd/Ctrl + S           save
 *   Cmd/Ctrl + H           find & replace panel
 *   Cmd/Ctrl + G           go to line
 * A status bar shows Ln/Col, selection length, the language, and the indent unit.
 */

const LINE_H = 21;
const PAD_Y = 12;
const INDENT = '  ';            // two spaces, the editor's indent unit
const PAIRS: Record<string, string> = { '(': ')', '[': ']', '{': '}', '"': '"', "'": "'", '`': '`' };
const COMMENT: Record<string, string> = {
  python: '#', bash: '#', sh: '#', shell: '#', yaml: '#', toml: '#', ruby: '#',
  javascript: '//', typescript: '//', js: '//', ts: '//', tsx: '//', jsx: '//',
  json: '//', css: '/*', go: '//', rust: '//', c: '//', java: '//',
};

export function CodeEditor({
  value, onChange, onSave, language,
}: {
  value: string;
  onChange: (next: string) => void;
  onSave?: () => void;
  /** File language (from the read API), for comment-toggle + the status bar. */
  language?: string;
}) {
  const taRef = useRef<HTMLTextAreaElement>(null);
  const gutterRef = useRef<HTMLDivElement>(null);
  const pendingSel = useRef<[number, number] | null>(null);
  const [caret, setCaret] = useState({ line: 1, col: 1, selLen: 0 });
  const [findOpen, setFindOpen] = useState(false);
  const [find, setFind] = useState('');
  const [replace, setReplace] = useState('');
  const findRef = useRef<HTMLInputElement>(null);

  const lines = useMemo(() => value.split('\n'), [value]);
  const lineCount = Math.max(1, lines.length);
  const commentTok = COMMENT[(language || '').toLowerCase()] ?? null;

  // Restore the caret/selection after a controlled edit that moved it.
  useLayoutEffect(() => {
    if (pendingSel.current && taRef.current) {
      const [s, e] = pendingSel.current;
      taRef.current.selectionStart = s;
      taRef.current.selectionEnd = e;
      pendingSel.current = null;
      updateCaret();
    }
  });

  const updateCaret = useCallback(() => {
    const ta = taRef.current;
    if (!ta) return;
    const pos = ta.selectionStart;
    const before = value.slice(0, pos);
    const line = before.split('\n').length;
    const col = pos - (before.lastIndexOf('\n') + 1) + 1;
    setCaret({ line, col, selLen: ta.selectionEnd - ta.selectionStart });
  }, [value]);

  function syncScroll() {
    if (gutterRef.current && taRef.current) gutterRef.current.scrollTop = taRef.current.scrollTop;
  }

  // Apply an edit + place the caret, going through onChange (controlled value).
  const apply = (next: string, selStart: number, selEnd = selStart) => {
    pendingSel.current = [selStart, selEnd];
    onChange(next);
  };

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    const ta = taRef.current;
    if (!ta) return;
    const mod = e.metaKey || e.ctrlKey;
    const start = ta.selectionStart;
    const end = ta.selectionEnd;
    const v = value;

    if (mod && e.key === 's') { e.preventDefault(); onSave?.(); return; }
    if (mod && (e.key === 'h' || e.key === 'H')) { e.preventDefault(); openFind(); return; }
    if (mod && (e.key === 'g' || e.key === 'G')) { e.preventDefault(); goToLine(); return; }
    if (mod && e.key === '/') { e.preventDefault(); toggleComment(start, end); return; }

    // Alt+Up/Down: move line(s); Shift+Alt+Down/Up: duplicate.
    if (e.altKey && (e.key === 'ArrowUp' || e.key === 'ArrowDown')) {
      e.preventDefault();
      if (e.shiftKey) duplicateLines(start, end);
      else moveLines(start, end, e.key === 'ArrowUp' ? -1 : 1);
      return;
    }

    if (e.key === 'Tab') {
      e.preventDefault();
      if (start === end && !e.shiftKey) { apply(v.slice(0, start) + INDENT + v.slice(end), start + INDENT.length); return; }
      indentBlock(start, end, e.shiftKey);
      return;
    }

    if (e.key === 'Enter') {
      e.preventDefault();
      const lineStart = v.lastIndexOf('\n', start - 1) + 1;
      const curLine = v.slice(lineStart, start);
      const lead = curLine.match(/^[ \t]*/)?.[0] ?? '';
      const opensBlock = /[:{[(]\s*$/.test(curLine.slice(0, start - lineStart));
      const extra = opensBlock ? INDENT : '';
      const ins = '\n' + lead + extra;
      apply(v.slice(0, start) + ins + v.slice(end), start + ins.length);
      return;
    }

    // Auto-close brackets/quotes; wrap a selection if one exists.
    if (PAIRS[e.key]) {
      const close = PAIRS[e.key]!;
      if (start !== end) {
        e.preventDefault();
        apply(v.slice(0, start) + e.key + v.slice(start, end) + close + v.slice(end), start + 1, end + 1);
        return;
      }
      // Don't double an already-balanced close char typed over itself.
      if (start === end) {
        e.preventDefault();
        apply(v.slice(0, start) + e.key + close + v.slice(end), start + 1);
        return;
      }
    }
    // Type over an auto-inserted close char instead of inserting a second one.
    if ((e.key === ')' || e.key === ']' || e.key === '}' || e.key === '"' || e.key === "'" || e.key === '`')
        && start === end && v[start] === e.key) {
      e.preventDefault();
      apply(v, start + 1);
      return;
    }
    // Backspace inside an empty pair removes both chars.
    if (e.key === 'Backspace' && start === end && start > 0 && PAIRS[v[start - 1]!] === v[start]) {
      e.preventDefault();
      apply(v.slice(0, start - 1) + v.slice(start + 1), start - 1);
      return;
    }
  }

  function indentBlock(start: number, end: number, dedent: boolean) {
    const v = value;
    const lineStart = v.lastIndexOf('\n', Math.max(0, start - 1)) + 1;
    const block = v.slice(lineStart, end);
    const ls = block.split('\n');
    if (dedent) {
      let first = 0, total = 0;
      const out = ls.map((ln, i) => {
        let rm = 0;
        if (ln.startsWith(INDENT)) rm = INDENT.length;
        else if (ln.startsWith(' ') || ln.startsWith('\t')) rm = 1;
        if (i === 0) first = rm;
        total += rm;
        return ln.slice(rm);
      }).join('\n');
      apply(v.slice(0, lineStart) + out + v.slice(end), Math.max(lineStart, start - first), end - total);
    } else {
      const out = ls.map((ln) => INDENT + ln).join('\n');
      apply(v.slice(0, lineStart) + out + v.slice(end), start + INDENT.length, end + INDENT.length * ls.length);
    }
  }

  function toggleComment(start: number, end: number) {
    if (!commentTok) return;
    const v = value;
    const lineStart = v.lastIndexOf('\n', Math.max(0, start - 1)) + 1;
    const lineEnd = v.indexOf('\n', end) === -1 ? v.length : v.indexOf('\n', end);
    const block = v.slice(lineStart, lineEnd);
    const ls = block.split('\n');
    const tok = commentTok;
    const allCommented = ls.filter((l) => l.trim()).every((l) => l.trim().startsWith(tok));
    const out = ls.map((l) => {
      if (!l.trim()) return l;
      if (allCommented) return l.replace(new RegExp(`^(\\s*)${tok.replace(/[.*+?^${}()|[\]\\/]/g, '\\$&')} ?`), '$1');
      const lead = l.match(/^\s*/)?.[0] ?? '';
      return lead + tok + ' ' + l.slice(lead.length);
    }).join('\n');
    apply(v.slice(0, lineStart) + out + v.slice(lineEnd), lineStart, lineStart + out.length);
  }

  function moveLines(start: number, end: number, dir: -1 | 1) {
    const v = value;
    const ls = v.split('\n');
    const a = v.slice(0, start).split('\n').length - 1;       // first line index
    const b = v.slice(0, end).split('\n').length - 1;          // last line index
    if (dir === -1 && a === 0) return;
    if (dir === 1 && b === ls.length - 1) return;
    const seg = ls.splice(a, b - a + 1);
    ls.splice(a + dir, 0, ...seg);
    const next = ls.join('\n');
    // Recompute the caret offset by counting chars up to the moved block's new top.
    const newTop = ls.slice(0, a + dir).join('\n').length + (a + dir > 0 ? 1 : 0);
    const blockLen = seg.join('\n').length;
    apply(next, newTop, newTop + blockLen);
  }

  function duplicateLines(start: number, end: number) {
    const v = value;
    const ls = v.split('\n');
    const a = v.slice(0, start).split('\n').length - 1;
    const b = v.slice(0, end).split('\n').length - 1;
    const seg = ls.slice(a, b + 1);
    ls.splice(b + 1, 0, ...seg);
    const next = ls.join('\n');
    const dupTop = ls.slice(0, b + 1).join('\n').length + 1;
    apply(next, dupTop, dupTop + seg.join('\n').length);
  }

  function goToLine() {
    const n = window.prompt(`Go to line (1-${lineCount}):`, String(caret.line));
    if (!n) return;
    const target = Math.min(lineCount, Math.max(1, parseInt(n, 10) || 1));
    const off = value.split('\n').slice(0, target - 1).join('\n').length + (target > 1 ? 1 : 0);
    apply(value, off, off);
    taRef.current?.focus();
  }

  function openFind() {
    const ta = taRef.current;
    if (ta && ta.selectionEnd > ta.selectionStart) setFind(value.slice(ta.selectionStart, ta.selectionEnd));
    setFindOpen(true);
    setTimeout(() => findRef.current?.focus(), 0);
  }

  const findNext = useCallback((backwards = false) => {
    const ta = taRef.current;
    if (!ta || !find) return;
    const hay = value.toLowerCase();
    const needle = find.toLowerCase();
    let idx: number;
    if (backwards) {
      idx = hay.lastIndexOf(needle, Math.max(0, ta.selectionStart - 1));
      if (idx < 0) idx = hay.lastIndexOf(needle);
    } else {
      idx = hay.indexOf(needle, ta.selectionEnd);
      if (idx < 0) idx = hay.indexOf(needle);
    }
    if (idx < 0) return;
    ta.focus();
    ta.selectionStart = idx;
    ta.selectionEnd = idx + find.length;
    updateCaret();
  }, [find, value, updateCaret]);

  function replaceCurrent() {
    const ta = taRef.current;
    if (!ta || !find) return;
    const sel = value.slice(ta.selectionStart, ta.selectionEnd);
    if (sel.toLowerCase() === find.toLowerCase()) {
      const at = ta.selectionStart;
      apply(value.slice(0, at) + replace + value.slice(ta.selectionEnd), at + replace.length);
      setTimeout(() => findNext(), 0);
    } else {
      findNext();
    }
  }

  function replaceAll() {
    if (!find) return;
    const re = new RegExp(find.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi');
    const next = value.replace(re, replace);
    if (next !== value) apply(next, Math.min(caret.line, next.length));
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col bg-background">
      {findOpen && (
        <div className="flex flex-col gap-1 border-b border-border bg-muted/30 px-2 py-1.5">
          <div className="flex items-center gap-1">
            <input
              ref={findRef}
              value={find}
              onChange={(e) => setFind(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') { e.preventDefault(); findNext(e.shiftKey); }
                if (e.key === 'Escape') { setFindOpen(false); taRef.current?.focus(); }
              }}
              placeholder="Find"
              className="h-6 min-w-0 flex-1 rounded border border-input bg-background px-2 font-mono text-[12px] outline-none focus:border-ring"
            />
            <button onClick={() => findNext(true)} title="Previous (Shift+Enter)" className="rounded p-1 hover:bg-accent"><ArrowUp className="size-3.5" /></button>
            <button onClick={() => findNext(false)} title="Next (Enter)" className="rounded p-1 hover:bg-accent"><ArrowDown className="size-3.5" /></button>
            <button onClick={() => { setFindOpen(false); taRef.current?.focus(); }} title="Close (Esc)" className="rounded p-1 hover:bg-accent"><X className="size-3.5" /></button>
          </div>
          <div className="flex items-center gap-1">
            <input
              value={replace}
              onChange={(e) => setReplace(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); replaceCurrent(); } }}
              placeholder="Replace"
              className="h-6 min-w-0 flex-1 rounded border border-input bg-background px-2 font-mono text-[12px] outline-none focus:border-ring"
            />
            <button onClick={replaceCurrent} title="Replace" className="rounded p-1 hover:bg-accent"><Replace className="size-3.5" /></button>
            <button onClick={replaceAll} title="Replace all" className="rounded px-1.5 py-1 text-[11px] hover:bg-accent">All</button>
          </div>
        </div>
      )}
      <div className="flex min-h-0 flex-1 overflow-hidden font-mono text-[13px]">
        <div
          ref={gutterRef}
          aria-hidden
          className="select-none overflow-hidden border-r border-border/60 bg-muted/20 px-2 text-right tabular-nums"
          style={{ lineHeight: `${LINE_H}px`, paddingTop: PAD_Y, paddingBottom: PAD_Y, minWidth: 44 }}
        >
          {Array.from({ length: lineCount }, (_, i) => (
            <div key={i} className={i + 1 === caret.line ? 'text-foreground/80' : 'text-muted-foreground/45'}>{i + 1}</div>
          ))}
        </div>
        <textarea
          ref={taRef}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          onScroll={syncScroll}
          onKeyDown={handleKeyDown}
          onKeyUp={updateCaret}
          onClick={updateCaret}
          onSelect={updateCaret}
          spellCheck={false}
          wrap="off"
          className="resize-none flex-1 bg-background px-3 leading-relaxed focus:outline-none"
          style={{ lineHeight: `${LINE_H}px`, paddingTop: PAD_Y, paddingBottom: PAD_Y, whiteSpace: 'pre', overflowWrap: 'normal' }}
        />
      </div>
      {/* Status bar: Ln/Col + selection length, language, indent unit (VS Code footer). */}
      <div className="flex items-center justify-end gap-4 border-t border-border bg-muted/20 px-3 py-0.5 text-[11px] text-muted-foreground">
        <span>Ln {caret.line}, Col {caret.col}{caret.selLen ? ` (${caret.selLen} selected)` : ''}</span>
        <span>Spaces: {INDENT.length}</span>
        <span className="uppercase">{language || 'text'}</span>
      </div>
    </div>
  );
}
