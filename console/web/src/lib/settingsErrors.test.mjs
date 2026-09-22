import assert from 'node:assert/strict';
import test from 'node:test';
import { ApiError, createAuthSession, SessionChangedError, SessionExpiredError } from './authSession.ts';
import { recordSettingsError, settingsErrorMessage } from './settingsErrors.ts';

const json = (value, status = 200) => new Response(JSON.stringify(value), { status });
const identity = signedIn => signedIn
  ? json({ authenticated: true, user_id: 'attendee' })
  : json({ authenticated: false, login_url: '/auth/login' }, 401);
const caught = promise => promise.then(() => assert.fail('Expected a rejected request'), error => error);

test('an expiry field message clears only after a successful explicit sign-in check', async () => {
  let signedIn = false;
  let unavailable = false;
  let writes = 0;
  const session = createAuthSession(async (path, init) => {
    if (path === '/api/auth/me') return unavailable ? new Response('', { status: 503 }) : identity(signedIn);
    if (init?.method === 'POST') writes++;
    return json({ error: 'unauthorized' }, 401);
  });
  const started = session.getSnapshot().revision;
  const field = recordSettingsError(undefined,
    await caught(session.request('/api/settings', { method: 'POST' })), started);
  await session.check();
  assert.ok(settingsErrorMessage(field, session.getSnapshot().revision));
  signedIn = true;
  assert.ok(settingsErrorMessage(field, session.getSnapshot().revision), 'Another tab signing in is insufficient');
  unavailable = true;
  await session.check();
  assert.ok(settingsErrorMessage(field, session.getSnapshot().revision), 'An unavailable identity check cannot clear it');
  unavailable = false;
  await session.check();
  assert.equal(settingsErrorMessage(field, session.getSnapshot().revision), '');
  assert.equal(writes, 1);
});

test('identical text, validation failures, and uncertain writes retain their actual provenance', () => {
  const expired = new SessionExpiredError();
  const reasons = [
    new Error(expired.message), expired.message, 'Enter the repository in owner/name format.',
    new ApiError(401, 'The upstream credential was rejected'), new ApiError(403, 'Write permission denied'),
    new TypeError('Failed to fetch'), new SessionChangedError(),
  ];
  for (const reason of reasons) {
    const field = recordSettingsError(undefined, reason, 0);
    assert.equal(settingsErrorMessage(field, 1), typeof reason === 'string' ? reason : reason.message);
  }
  assert.equal(settingsErrorMessage(recordSettingsError(undefined, expired, 0), 1), '');
});

test('settings reads cannot erase action errors, but a successful read resolves its own failure', () => {
  let action = recordSettingsError(undefined, 'Invalid repository draft', 0);
  action = recordSettingsError(action, '', 1, 'read');
  action = recordSettingsError(action, new ApiError(503, 'Settings unavailable'), 1, 'read');
  assert.equal(settingsErrorMessage(action, 1), 'Invalid repository draft');
  const failedRead = recordSettingsError(undefined, new ApiError(503, 'Settings unavailable'), 0, 'read');
  assert.equal(settingsErrorMessage(failedRead, 1), 'Settings unavailable');
  assert.equal(settingsErrorMessage(recordSettingsError(failedRead, '', 1, 'read'), 1), '');
  assert.equal(settingsErrorMessage(recordSettingsError(action, '', 1), 1), '', 'An explicit edit retry can clear its previous error');
});

test('a late rejected request cannot restore an already-resolved expiry message', async () => {
  let signedIn = false;
  let finishLate;
  const lateResponse = new Promise(resolve => { finishLate = resolve; });
  const session = createAuthSession(async path => {
    if (path === '/api/auth/me') return identity(signedIn);
    if (path === '/api/late') return lateResponse;
    return json({ error: 'unauthorized' }, 401);
  });
  const started = session.getSnapshot().revision;
  const late = caught(session.request('/api/late'));
  await caught(session.request('/api/expire'));
  await session.check();
  signedIn = true;
  await session.check();
  finishLate(json({ error: 'unauthorized' }, 401));
  const error = await late;
  assert.ok(error instanceof SessionExpiredError);
  assert.equal(session.getSnapshot().expired, false);
  assert.equal(settingsErrorMessage(recordSettingsError(undefined, error, started), session.getSnapshot().revision), '');
});

test('a later expiry does not resurrect an old field error or suppress the current one', () => {
  const oldField = recordSettingsError(undefined, new SessionExpiredError(), 0);
  assert.equal(settingsErrorMessage(oldField, 1), '');
  const newField = recordSettingsError(undefined, new SessionExpiredError(), 1);
  assert.ok(settingsErrorMessage(newField, 1));
  assert.equal(settingsErrorMessage(newField, 2), '');
});
