import assert from 'node:assert/strict';
import test from 'node:test';
import { ApiError, getAuthMe } from '../api.ts';

test('a cleared session preserves the login destination instead of becoming a local session', async t => {
  for (const login_url of ['/auth/login', '/console/']) {
    t.mock.method(globalThis, 'fetch', async () => new Response(
      JSON.stringify({ authenticated: false, login_url }), { status: 401 },
    ));
    assert.deepEqual(await getAuthMe(), { authenticated: false, login_url });
    t.mock.restoreAll();
  }
});

test('an intentionally open local console does not ask the user to sign in', async t => {
  t.mock.method(globalThis, 'fetch', async () => new Response(
    JSON.stringify({ authenticated: false, mode: 'local' }),
  ));
  assert.deepEqual(await getAuthMe(), { authenticated: false, mode: 'local' });
});

test('an unavailable auth service is reported instead of becoming an anonymous identity', async t => {
  t.mock.method(globalThis, 'fetch', async () => new Response('Unavailable', { status: 503 }));
  await assert.rejects(getAuthMe(), error => error instanceof ApiError && error.status === 503);
});
