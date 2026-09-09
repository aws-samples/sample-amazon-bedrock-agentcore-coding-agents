import type { RolePrEntry, RunDetail, RunResult } from '../api';

type RunEvidence = Pick<RunResult, 'status' | 'role_prs' | 'work_items' | 'pr_url' | 'merge_state'>;
export type DecisionTone = 'success' | 'waiting' | 'danger' | 'neutral';

/** Use recorded URLs only. Older runs may have the single-PR fields alone. */
export function recordedPullRequests(run: RunEvidence): RolePrEntry[] {
  if (run.role_prs?.length) return run.role_prs;
  const builders = Object.values(run.work_items ?? {}).filter(
    (item) => item.kind === 'builder' && item.pr?.pr_url,
  );
  if (builders.length) {
    return builders.map((item) => ({
      work_id: item.work_id,
      agent: item.agent,
      role: item.capability,
      pr_url: item.pr!.pr_url,
      state: item.merge_state ?? item.state,
    }));
  }
  return run.pr_url
    ? [{ work_id: 'recorded-pr', agent: '', role: 'Pull request', pr_url: run.pr_url, state: run.merge_state ?? 'unknown' }]
    : [];
}

/**
 * Presentation of persisted facts, never a new verdict. A passing engine run
 * under human_review is not a merge; one merged sibling is not a merged team.
 */
export function presentRunDecision(run: RunEvidence): { title: string; detail: string; tone: DecisionTone } {
  const rows = recordedPullRequests(run);
  const merged = rows.filter((row) => row.state === 'merged').length;
  const awaiting = rows.filter((row) => row.state === 'awaiting_review').length;
  const blocked = rows.filter((row) => row.state === 'blocked').length;
  const pr = (n: number) => `${n} pull request${n === 1 ? '' : 's'}`;

  if (run.status === 'failed' || run.status === 'needs_human' || blocked > 0) {
    return {
      title: run.status === 'failed' ? 'Run failed' : 'Needs your attention',
      detail: merged
        ? `${pr(merged)} merged. Inspect the remaining PR evidence and the recorded next action.`
        : 'Inspect the check, review, and recorded next action before continuing.',
      tone: 'danger',
    };
  }
  if (run.status === 'passed') {
    if (rows.length > 0 && merged === rows.length) {
      return { title: 'Pull requests merged', detail: `${pr(merged)} recorded as merged.`, tone: 'success' };
    }
    if (awaiting > 0) {
      return {
        title: 'Ready for your review',
        detail: `${pr(awaiting)} awaiting your decision.${merged ? ` ${pr(merged)} already merged.` : ' Read the evidence, then merge acceptable work.'}`,
        tone: 'waiting',
      };
    }
    if (rows.some((row) => row.pr_url)) {
      return { title: 'Checks completed', detail: 'Inspect the recorded outcome on each pull request.', tone: 'waiting' };
    }
    return {
      title: 'Checks completed · no PR recorded',
      detail: 'A passing check did not publish the work to GitHub. Follow the recorded next action.',
      tone: 'waiting',
    };
  }
  return { title: 'Build in progress', detail: 'The evidence below updates as the team works.', tone: 'neutral' };
}

export function gateResultLabel(gate: RunDetail['gate'] | RunResult['gate']): string {
  return gate == null ? 'Not recorded' : gate.passed ? 'Passed' : 'Failed';
}
