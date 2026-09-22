import assert from 'node:assert/strict';
import test from 'node:test';
import { createAuthSession, SessionChangedError, SessionExpiredError } from './authSession.ts';
import { connectionStatus } from './connectionStatus.ts';

const attendee = { authenticated: true, user_id: 'attendee-sub', email: 'attendee@example.test' };
const json = (body, status = 200) => new Response(JSON.stringify(body), { status });
const expired = () => json({ authenticated: false, login_url: '/auth/login' }, 401);
const deferred = () => {
  let resolve;
  const promise = new Promise(accept => { resolve = accept; });
  return { promise, resolve };
};
const checked = async session => {
  if (!session.getSnapshot().checking) return;
  await new Promise(resolve => {
    const unsubscribe = session.subscribe(() => {
      if (!session.getSnapshot().checking) { unsubscribe(); resolve(); }
    });
  });
};

test('a restarted console expires its old identity without claiming the repository is absent', async () => {
  const calls = [];
  let signedIn = true;
  const session = createAuthSession(async (path, init) => {
    calls.push({ path, init });
    if (path === '/api/auth/me') return signedIn ? json(attendee) : expired();
    return json({ error: 'unauthorized' }, 401);
  });
  await session.check();
  assert.deepEqual(session.getSnapshot().user, attendee);
  signedIn = false;

  await assert.rejects(session.request('/api/orchestrator/github'), SessionExpiredError);
  await checked(session);
  const state = session.getSnapshot();
  assert.equal(state.user, null);
  assert.equal(state.expired, true);
  assert.equal(state.loginUrl, '/auth/login');
  assert.deepEqual(connectionStatus(false, state.expired, null), { type: 'error', label: 'Session expired' });
  assert.deepEqual(calls.map(call => call.path), ['/api/auth/me', '/api/orchestrator/github', '/api/auth/me']);
  assert.ok(calls.every(call => !call.init?.method));
});

test('failed writes stay failed and are never replayed when the user checks a new sign-in', async () => {
  const calls = [];
  let signedIn = false;
  const body = JSON.stringify({ repo: 'owner/edited-repository', merge_policy: 'human_review' });
  const session = createAuthSession(async (path, init) => {
    calls.push({ path, init });
    if (path === '/api/auth/me') return signedIn ? json(attendee) : expired();
    return signedIn ? json({ connected: true, repo: 'owner/edited-repository' }) : json({ error: 'unauthorized' }, 401);
  });
  await assert.rejects(session.request('/api/orchestrator/github', { method: 'POST', body }), SessionExpiredError);
  await checked(session);
  const count = calls.length;
  await assert.rejects(session.request('/api/orchestrator/chat', { method: 'POST', body: '{"prompt":"build"}' }), SessionExpiredError);
  assert.equal(calls.length, count, 'Known expiry must not issue another operation');

  signedIn = true;
  await session.check();
  assert.equal(session.getSnapshot().expired, false);
  assert.deepEqual(session.getSnapshot().user, attendee);
  assert.equal(session.getSnapshot().revision, 1);
  assert.equal(calls.filter(call => call.init?.method === 'POST').length, 1);
  assert.equal(calls.find(call => call.init?.method === 'POST').init.body, body);
  assert.equal(calls.some(call => call.path === '/api/orchestrator/chat'), false);
  // Only a separate, explicit operation after recovery can save the draft.
  const response = await session.request('/api/orchestrator/github', { method: 'POST', body });
  assert.equal(response.status, 200);
  assert.equal(calls.filter(call => call.init?.method === 'POST').length, 2);
});

test('another tab signing in does not resume operations until Check sign-in is chosen', async () => {
  const session = createAuthSession(async path =>
    path === '/api/auth/me' ? json(attendee) : json({ error: 'unauthorized' }, 401));
  await assert.rejects(session.request('/api/orchestrator/github'), SessionExpiredError);
  await checked(session);
  assert.equal(session.getSnapshot().expired, true);
  assert.equal(session.getSnapshot().user, null);
  await session.check();
  assert.equal(session.getSnapshot().expired, false);
  assert.deepEqual(session.getSnapshot().user, attendee);
});

test('concurrent 401s resolve one login destination and late responses cannot undo a new sign-in', async () => {
  const responses = new Map();
  const calls = [];
  let signedIn = false;
  const session = createAuthSession(async path => {
    calls.push(path);
    if (path === '/api/auth/me') return signedIn ? json(attendee) : expired();
    const response = deferred();
    responses.set(path, response);
    return response.promise;
  });
  const github = session.request('/api/orchestrator/github');
  const runtimes = session.request('/api/orchestrator/runtimes');
  const late = session.request('/api/metrics/attribution');
  responses.get('/api/orchestrator/github').resolve(json({ error: 'unauthorized' }, 401));
  await assert.rejects(github, SessionExpiredError);
  responses.get('/api/orchestrator/runtimes').resolve(json({ error: 'unauthorized' }, 401));
  await assert.rejects(runtimes, SessionExpiredError);
  await checked(session);
  assert.equal(calls.filter(path => path === '/api/auth/me').length, 1);

  signedIn = true;
  await session.check();
  responses.get('/api/metrics/attribution').resolve(json({ error: 'unauthorized' }, 401));
  await assert.rejects(late, SessionExpiredError);
  assert.equal(session.getSnapshot().expired, false, 'An old request must not expire the recovered sign-in');
  assert.deepEqual(session.getSnapshot().user, attendee);
  assert.equal(calls.filter(path => path === '/api/auth/me').length, 2);
});

test('a delayed successful configuration response is not accepted across a session failure', async () => {
  const late = deferred();
  const session = createAuthSession(async path => {
    if (path === '/api/orchestrator/github') return late.promise;
    if (path === '/api/auth/me') return expired();
    return json({ error: 'unauthorized' }, 401);
  });
  const github = session.request('/api/orchestrator/github');
  await assert.rejects(session.request('/api/orchestrator/runtimes'), SessionExpiredError);
  late.resolve(json({ connected: true, repo: 'owner/old-repository' }));
  await assert.rejects(github, SessionChangedError);
  await checked(session);
  assert.equal(session.getSnapshot().expired, true);
});

test('a write crossing a sign-in change has an uncertain outcome, not a rejected request ready to resend', async () => {
  const result = deferred();
  let writes = 0;
  let signedIn = false;
  const session = createAuthSession(async (path, init) => {
    if (path === '/api/auth/me') return signedIn ? json(attendee) : expired();
    if (init?.method === 'POST') { writes++; return result.promise; }
    return json({ error: 'unauthorized' }, 401);
  });
  const write = session.request('/api/orchestrator/chat', { method: 'POST', body: '{"prompt":"build"}' });
  await assert.rejects(session.request('/api/orchestrator/github'), SessionExpiredError);
  await checked(session);
  signedIn = true;
  await session.check();
  result.resolve(json({ accepted: true }));
  await assert.rejects(write, error => error instanceof SessionChangedError && !(error instanceof SessionExpiredError));
  assert.equal(writes, 1);
  assert.equal(session.getSnapshot().expired, false);
});

test('the initial identity check cannot restore an old header after a newer 401', async () => {
  const oldIdentity = deferred();
  let identityReads = 0;
  const session = createAuthSession(async path => {
    if (path === '/api/auth/me') return ++identityReads === 1 ? oldIdentity.promise : expired();
    return json({ error: 'unauthorized' }, 401);
  });
  const initial = session.check();
  await assert.rejects(session.request('/api/orchestrator/github'), SessionExpiredError);
  await checked(session);
  oldIdentity.resolve(json(attendee));
  await initial;
  assert.equal(session.getSnapshot().expired, true);
  assert.equal(session.getSnapshot().user, null);
  assert.equal(session.getSnapshot().checking, false);
  assert.equal(session.getSnapshot().loginUrl, '/auth/login');
});

test('403, service errors and aborted requests are not relabeled as session expiry or retried', async () => {
  for (const status of [403, 404, 500, 503]) {
    let calls = 0;
    const response = json({ error: 'request failed' }, status);
    const session = createAuthSession(async () => { calls++; return response; });
    assert.equal(await session.request('/api/orchestrator/github'), response);
    assert.equal(calls, 1);
    assert.equal(session.getSnapshot().expired, false);
  }
  const ac = new AbortController();
  ac.abort();
  const session = createAuthSession(async (_path, init) => {
    assert.equal(init.signal, ac.signal);
    throw ac.signal.reason;
  });
  await assert.rejects(session.request('/api/orchestrator/chat', { method: 'POST', signal: ac.signal }), { name: 'AbortError' });
  assert.equal(session.getSnapshot().expired, false);
});

test('unavailable identity verification cannot resume an expired session', async () => {
  let unavailable = false;
  const session = createAuthSession(async path => {
    if (path === '/api/auth/me') return unavailable ? new Response('Unavailable', { status: 503 }) : expired();
    return json({ error: 'unauthorized' }, 401);
  });
  await assert.rejects(session.request('/api/orchestrator/github'), SessionExpiredError);
  await checked(session);
  unavailable = true;
  await session.check();
  assert.equal(session.getSnapshot().expired, true);
  assert.equal(session.getSnapshot().user, null);
  assert.match(session.getSnapshot().error, /503/);
  assert.equal(session.getSnapshot().loginUrl, '/auth/login');
});

test('open local mode remains open; password mode preserves its own login destination', async () => {
  const local = createAuthSession(async () => json({ authenticated: false, mode: 'local' }));
  await local.check();
  assert.equal(local.getSnapshot().expired, false);
  assert.equal(local.getSnapshot().loginUrl, null);
  assert.equal(local.getSnapshot().user.mode, 'local');
  const password = createAuthSession(async () => json({ authenticated: false, login_url: '/console/' }, 401));
  await password.check();
  assert.equal(password.getSnapshot().expired, true);
  assert.equal(password.getSnapshot().loginUrl, '/console/');
});

test('invalid sign-in responses cannot invent an open session or external sign-in link', async () => {
  for (const body of [
    {},
    { authenticated: false },
    { authenticated: true, login_url: '/auth/login' },
    ...['https://example.test/login', '//example.test/login', '/\\example.test/login', '/\n/example.test/login', 'javascript:alert(1)']
      .map(login_url => ({ authenticated: false, login_url })),
  ]) {
    const session = createAuthSession(async () => json(body, 401));
    await session.check();
    assert.equal(session.getSnapshot().user, null);
    assert.equal(session.getSnapshot().loginUrl, null);
    assert.equal(session.getSnapshot().expired, true);
    assert.ok(session.getSnapshot().error);
  }
  const unverified = createAuthSession(async () => json({ authenticated: false }));
  await unverified.check();
  assert.equal(unverified.getSnapshot().user, null);
  assert.ok(unverified.getSnapshot().error);
});

test('configuration requires an explicit successful read before it can be called unconfigured', () => {
  assert.deepEqual(connectionStatus(false, false, null), { type: 'error', label: 'Unable to verify' });
  assert.deepEqual(connectionStatus(false, false, { connected: null }), { type: 'error', label: 'Unable to verify' });
  assert.deepEqual(connectionStatus(false, false, { connected: true }, true), { type: 'error', label: 'Unable to verify' });
  assert.deepEqual(connectionStatus(false, false, { connected: false, error: 'Access denied' }), { type: 'error', label: 'Unable to verify' });
  assert.deepEqual(connectionStatus(true, false, null), { type: 'loading', label: 'Loading' });
  assert.deepEqual(connectionStatus(false, false, { connected: false }), { type: 'pending', label: 'Not configured' });
  assert.deepEqual(connectionStatus(false, false, { connected: true }), { type: 'success', label: 'Configured' });
});
