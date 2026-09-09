import assert from 'node:assert/strict';
import test from 'node:test';
import { presentRunDecision, recordedPullRequests, gateResultLabel } from './runPresentation.ts';

const row = (state, id = 'backend') => ({
  work_id: id, agent: id, role: id, state, pr_url: `https://github.com/example/project/pull/${id}`,
});

test('a passing human-review run never claims that its PR merged', () => {
  const result = presentRunDecision({ status: 'passed', role_prs: [row('awaiting_review')] });
  assert.equal(result.title, 'Ready for your review');
  assert.equal(result.tone, 'waiting');
  assert.doesNotMatch(result.detail, /recorded as merged/);
});

test('one merged sibling does not complete the remaining PR', () => {
  const result = presentRunDecision({
    status: 'passed', role_prs: [row('merged'), row('awaiting_review', 'frontend')],
  });
  assert.equal(result.title, 'Ready for your review');
  assert.match(result.detail, /1 pull request awaiting/);
  assert.match(result.detail, /1 pull request already merged/);
});

test('blocked work remains visible after a sibling merges', () => {
  const result = presentRunDecision({
    status: 'needs_human', role_prs: [row('merged'), row('blocked', 'frontend')],
  });
  assert.equal(result.tone, 'danger');
  assert.match(result.detail, /1 pull request merged/);
  assert.match(result.detail, /remaining PR evidence/);
});

test('only a complete recorded merge earns the merged headline', () => {
  assert.equal(presentRunDecision({
    status: 'passed', role_prs: [row('merged'), row('merged', 'frontend')],
  }).title, 'Pull requests merged');
  assert.notEqual(presentRunDecision({ status: 'passed', role_prs: [] }).tone, 'success');
});

test('a missing check is distinct from a failed check', () => {
  assert.equal(gateResultLabel(undefined), 'Not recorded');
  assert.equal(gateResultLabel(null), 'Not recorded');
  assert.equal(gateResultLabel({ passed: false }), 'Failed');
  assert.equal(gateResultLabel({ passed: true }), 'Passed');
});

test('a successful run without a published PR says so', () => {
  const result = presentRunDecision({ status: 'passed' });
  assert.match(result.title, /no PR recorded/);
  assert.deepEqual(recordedPullRequests({ status: 'passed' }), []);
});

test('historic single-PR records keep their actual URL and merge state', () => {
  const run = { status: 'passed', pr_url: 'https://github.com/example/project/pull/42', merge_state: 'awaiting_review' };
  assert.equal(recordedPullRequests(run)[0].pr_url, run.pr_url);
  assert.equal(presentRunDecision(run).title, 'Ready for your review');
});
