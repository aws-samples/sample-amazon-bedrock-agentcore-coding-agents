import { useId } from 'react';
import { Route, SquareTerminal, FileCheck2, GitPullRequest } from 'lucide-react';

/** A teaching illustration, deliberately independent of run status. */
export function LoopIllustration() {
  const id = useId().replaceAll(':', '');
  return (
    <figure className="loop-illustration">
      <div className="mb-3 flex items-center justify-between gap-3">
        <span className="eyebrow">How a build works</span>
        <span className="text-xs text-muted-foreground">One PR per builder</span>
      </div>
      <svg viewBox="0 0 520 304" role="img" aria-labelledby={`${id}-title ${id}-desc`}>
        <title id={`${id}-title`}>Plan, build, check, and decide</title>
        <desc id={`${id}-desc`}>
          The coordinator plans a request. A builder works in its own checkout.
          An executable check and independent review produce evidence for a decision.
          Findings allow one repair on the same pull request, then return to a person.
          This diagram explains the workflow; it does not display a running task.
        </desc>
        <defs>
          {(['blue', 'purple', 'green'] as const).map((tone) => (
            <marker key={tone} id={`${id}-${tone}`} viewBox="0 0 8 8" refX="7" refY="4"
              markerWidth="7" markerHeight="7" orient="auto-start-reverse">
              <path d="M1 1 L7 4 L1 7" fill="none" stroke={`var(--loop-${tone})`} strokeWidth="1.5" />
            </marker>
          ))}
        </defs>
        <g fill="none" className="loop-path" stroke="currentColor" strokeWidth="1.5"
          markerEnd={`url(#${id}-blue)`}>
          <path d="M232 67 H291" />
          <path className="loop-check" d="M400 116 V183" markerEnd={`url(#${id}-purple)`} />
          <path className="loop-decision" d="M296 237 H237" markerEnd={`url(#${id}-green)`} />
          <path className="loop-repair" d="M296 213 C258 213 258 99 291 99" markerEnd={`url(#${id}-purple)`} />
        </g>
        <g className="loop-node">
          <rect x="16" y="18" width="216" height="98" rx="12" />
          <Route x="33" y="36" width="23" height="23" className="loop-icon" />
          <text x="67" y="54" className="loop-title">Plan</text>
          <text x="34" y="91" className="loop-caption">A goal and shared requirements</text>
        </g>
        <g className="loop-node loop-node-builder">
          <rect x="296" y="18" width="208" height="98" rx="12" />
          <SquareTerminal x="312" y="36" width="23" height="23" className="loop-icon" />
          <text x="346" y="54" className="loop-title">Build</text>
          <text x="313" y="91" className="loop-caption">Own checkout. Own pull request.</text>
        </g>
        <g className="loop-node loop-node-checker">
          <rect x="296" y="188" width="208" height="98" rx="12" />
          <FileCheck2 x="312" y="206" width="23" height="23" className="loop-icon" />
          <text x="346" y="224" className="loop-title">Check &amp; review</text>
          <text x="313" y="261" className="loop-caption">Independent, executable evidence</text>
        </g>
        <g className="loop-node loop-node-decision">
          <rect x="16" y="188" width="216" height="98" rx="12" />
          <GitPullRequest x="33" y="206" width="23" height="23" className="loop-icon" />
          <text x="67" y="224" className="loop-title">Your decision</text>
          <text x="34" y="261" className="loop-caption">Inspect the result and act</text>
        </g>
        <text x="126" y="155" className="loop-repair-label">One repair per PR</text>
        <text x="126" y="172" className="loop-caption">then a human handoff</text>
      </svg>
    </figure>
  );
}
