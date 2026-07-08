import { useCallback, useEffect, useRef, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { GitBranch, GitPullRequest, Check, AlertCircle, Loader2 } from 'lucide-react';
import { listRunsPaged, type RunSummary } from '../api';

const PAGE = 20;

// The state chip on each session row (Copilot-app pattern: a small glyph that
// reads the run's REAL status at a glance). A PR-opened run shows the PR glyph
// over the plain check, since "opened a pull request" is the outcome that matters.
function SessionChip({ run }: { run: RunSummary }) {
  const terminal = ['passed', 'failed', 'needs_human'].includes(run.status);
  if (!terminal) {
    return <Loader2 className="size-3 shrink-0 animate-spin text-muted-foreground" aria-label="working" />;
  }
  if (run.status === 'passed') {
    return run.pr_url
      ? <GitPullRequest className="size-3 shrink-0 text-emerald-500" aria-label="pull request opened" />
      : <Check className="size-3 shrink-0 text-emerald-500" aria-label="passed" />;
  }
  // failed | needs_human: amber for needs_human (recoverable), red for failed.
  return (
    <AlertCircle
      className={`size-3 shrink-0 ${run.status === 'needs_human' ? 'text-amber-500' : 'text-destructive'}`}
      aria-label={run.status}
    />
  );
}
// While any run is in flight we re-poll the first page so the list reflects new
// runs + status changes; once everything settled we poll lazily.
const ACTIVE_POLL_MS = 2000;

/**
 * The Sessions list, inline under the "Chat" nav item (the Copilot-app pattern:
 * each run IS a session, newest first, with a branch glyph + a state chip that
 * reads the run's real status). A short scroll container (hidden scrollbar,
 * bottom fade) that infinite-scrolls older sessions as you reach the end. The
 * active session is highlighted by background. Original, workshop-simplified.
 */
export function SidebarRunList() {
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const activeId = pathname.startsWith('/fleets/') ? pathname.slice('/fleets/'.length) : null;

  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [total, setTotal] = useState(0);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const loadingMore = useRef(false);
  // Whether any run is still in flight, tracked in a ref (updated inside
  // refreshHead) so the polling effect does NOT depend on `runs`. Depending on
  // `runs` made every setRuns() re-fire the effect, which re-created the interval
  // and re-fetched immediately, an infinite /runs?limit=20&offset=0 loop.
  const anyLiveRef = useRef(false);

  // Refresh the first page (newest runs); merges into existing list so a
  // scrolled-down user doesn't get yanked back, but new runs appear up top.
  const refreshHead = useCallback(async () => {
    try {
      const { runs: head, total: t } = await listRunsPaged(PAGE, 0);
      setTotal(t);
      setRuns((prev) => {
        const byId = new Map(prev.map((r) => [r.run_id, r]));
        for (const r of head) byId.set(r.run_id, r);
        // newest-first: the API already returns newest first; sort by id desc
        // (ids are timestamp-ordered) to keep a stable merged order.
        const merged = Array.from(byId.values()).sort((a, b) => (a.run_id < b.run_id ? 1 : -1));
        anyLiveRef.current = merged.some(
          (r) => !['passed', 'failed', 'needs_human'].includes(r.status));
        return merged;
      });
    } catch { /* keep what we have */ }
  }, []);

  const loadMore = useCallback(async () => {
    if (loadingMore.current) return;
    if (runs.length >= total && total > 0) return;
    loadingMore.current = true;
    try {
      const { runs: page, total: t } = await listRunsPaged(PAGE, runs.length);
      setTotal(t);
      setRuns((prev) => {
        const byId = new Map(prev.map((r) => [r.run_id, r]));
        for (const r of page) byId.set(r.run_id, r);
        return Array.from(byId.values()).sort((a, b) => (a.run_id < b.run_id ? 1 : -1));
      });
    } catch { /* ignore */ } finally {
      loadingMore.current = false;
    }
  }, [runs.length, total]);

  // Poll the first page on a SELF-SCHEDULING timeout that depends only on the
  // stable refreshHead, never on `runs` (that caused an infinite refetch). The
  // cadence reads anyLiveRef: faster while a run is in flight, lazy once settled.
  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    const tick = async () => {
      await refreshHead();
      if (stopped) return;
      timer = setTimeout(tick, anyLiveRef.current ? ACTIVE_POLL_MS : ACTIVE_POLL_MS * 3);
    };
    tick();
    return () => { stopped = true; clearTimeout(timer); };
  }, [refreshHead]);

  // Infinite scroll: a plain scroll listener (an IntersectionObserver on a nested
  // scroll root is flaky to mount); load more when within 60px of the bottom.
  const onScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    if (el.scrollHeight - el.scrollTop - el.clientHeight < 60) loadMore();
  }, [loadMore]);

  if (runs.length === 0) return null;

  return (
    <div className="relative ml-7 mr-1 mt-0.5">
      <div
        ref={scrollRef}
        onScroll={onScroll}
        className="max-h-52 space-y-px overflow-y-auto pr-1 [-ms-overflow-style:none] [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
      >
        {runs.map((r) => {
          const active = r.run_id === activeId;
          const label = r.task.length > 30 ? r.task.slice(0, 28) + '…' : r.task;
          return (
            <button
              key={r.run_id}
              onClick={() => navigate(`/fleets/${r.run_id}`)}
              title={r.task}
              className={[
                'flex w-full items-center gap-1.5 rounded-md px-2 py-1 text-left text-xs transition-colors',
                active
                  ? 'bg-sidebar-accent text-sidebar-accent-foreground'
                  : 'text-muted-foreground hover:bg-sidebar-accent/50 hover:text-foreground',
              ].join(' ')}
            >
              <GitBranch className="size-3 shrink-0 opacity-60" />
              <span className="min-w-0 flex-1 truncate">{label}</span>
              <SessionChip run={r} />
            </button>
          );
        })}
      </div>
      {/* Bottom fade so the cutoff reads as "more below", not a hard edge. */}
      {runs.length < total && (
        <div className="pointer-events-none absolute inset-x-0 bottom-0 h-5 bg-gradient-to-t from-sidebar to-transparent" />
      )}
    </div>
  );
}
