import assert from 'node:assert/strict';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test, { after } from 'node:test';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';

// Exercise the real store, including its storage and API boundary. Requiring a
// fresh bundle resets module state like a page reload without resetting storage.
const web = fileURLToPath(new URL('../../', import.meta.url));
const directory = await mkdtemp(path.join(tmpdir(), 'session-store-test-'));
after(() => rm(directory, { recursive: true, force: true }));
const bundled = await build({
  entryPoints: [path.join(web, 'src/hooks/useSessionStore.ts')],
  tsconfig: path.join(web, 'tsconfig.app.json'),
  bundle: true, platform: 'node', format: 'cjs', write: false,
  loader: { '.css': 'empty', '.svg': 'dataurl', '.png': 'dataurl' },
  logLevel: 'silent',
});
const modulePath = path.join(directory, 'store.cjs');
await writeFile(modulePath, bundled.outputFiles[0].contents);
const require = createRequire(import.meta.url);
const key = 'agentcore.console.sessions';
const chat = (overrides = {}) => ({
  session_id: 'console-codex-chat', agent_id: 'codex',
  runtime_arn: 'test-codex-runtime', alive: true, opened_by: 'orchestrator',
  ...overrides,
});
const savedTab = (overrides = {}) => ({
  id: 'console-codex-chat', agentId: 'codex',
  runtimeArn: 'test-codex-runtime', label: 7, ...overrides,
});

function environment(t, saved) {
  let raw = saved === undefined ? null : JSON.stringify(saved);
  let writes = 0;
  let rows = [];
  let status = 200;
  const calls = [];
  const storage = {
    getItem(name) { assert.equal(name, key); return raw; },
    setItem(name, value) { assert.equal(name, key); raw = value; writes++; },
  };
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage');
  Object.defineProperty(globalThis, 'sessionStorage', { configurable: true, value: storage });
  t.after(() => {
    if (descriptor) Object.defineProperty(globalThis, 'sessionStorage', descriptor);
    else delete globalThis.sessionStorage;
    assert.ok(calls.every(call => call.method === 'GET'), 'Reload and sync must never start or restart a worker');
  });
  t.mock.method(globalThis, 'fetch', async (url, init) => {
    calls.push({ url, method: init?.method ?? 'GET' });
    assert.equal(url, '/api/dev/runtime-sessions?agent_id=codex');
    return new Response(JSON.stringify({ sessions: rows }), { status });
  });
  return {
    reload() { delete require.cache[require.resolve(modulePath)]; return require(modulePath); },
    respond(sessions, responseStatus = 200) { rows = sessions; status = responseStatus; },
    get saved() { return raw === null ? null : JSON.parse(raw); },
    get writes() { return writes; },
    calls, storage,
  };
}

test('a Chat tab retains its origin, ID and stable number through a full reload', async t => {
  const env = environment(t);
  const manual = chat({ session_id: 'console-codex-manual', opened_by: 'user' });
  env.respond([manual, chat()]);
  const store = env.reload();
  assert.equal(await store.syncServerSessions('codex'), true);
  assert.deepEqual(store.getSessions('codex').map(({ label, openedBy }) => ({ label, openedBy })), [
    { label: 1, openedBy: 'user' }, { label: 2, openedBy: 'orchestrator' },
  ]);

  // Removing the earlier tab must not renumber the Chat tab.
  env.respond([chat()]);
  assert.equal(await store.syncServerSessions('codex'), true);
  const expected = { ...savedTab({ label: 2, openedBy: 'orchestrator' }), alive: true };
  const requestsBeforeReload = env.calls.length;
  const reloaded = env.reload();
  assert.notEqual(reloaded, store, 'A reload must discard the previous module state');
  assert.deepEqual(reloaded.getSessions('codex'), [expected]);
  assert.equal(env.calls.length, requestsBeforeReload, 'Hydration only reads local storage');
  const writesBeforeSync = env.writes;
  assert.equal(await reloaded.syncServerSessions('codex'), false);
  assert.equal(env.writes, writesBeforeSync, 'Unchanged metadata should not rewrite storage');

  env.respond([chat(), chat({ session_id: 'console-codex-next' })]);
  assert.equal(await reloaded.syncServerSessions('codex'), true);
  assert.deepEqual(reloaded.getSessions('codex').map(({ id, label }) => ({ id, label })), [
    { id: expected.id, label: 2 }, { id: 'console-codex-next', label: 3 },
  ]);
});

test('an older stored tab learns its origin from the server and keeps it on the next reload', async t => {
  const env = environment(t, { rows: [savedTab()], seq: [['codex', 9]] });
  env.respond([chat()]);
  const store = env.reload();
  const [existing] = store.getSessions('codex');
  assert.equal(existing.openedBy, undefined);

  assert.equal(await store.syncServerSessions('codex'), true);
  assert.equal(store.getSession(existing.id), existing, 'Reconcile the existing tab instead of replacing it');
  assert.deepEqual(existing, { ...savedTab({ openedBy: 'orchestrator' }), alive: true });
  assert.deepEqual(env.saved, {
    rows: [savedTab({ openedBy: 'orchestrator' })], seq: [['codex', 9]],
  });
  const reloaded = env.reload();
  assert.deepEqual(reloaded.getSessions('codex'), [existing]);
  env.respond([chat(), chat({ session_id: 'console-codex-next' })]);
  assert.equal(await reloaded.syncServerSessions('codex'), true);
  assert.equal(reloaded.getSession('console-codex-next').label, 10);
});

for (const [stored, authoritative] of [['user', 'orchestrator'], ['orchestrator', 'user']]) {
  test(`server origin ${authoritative} corrects a stored ${stored} marker without replacing the tab`, async t => {
    const env = environment(t, {
      rows: [savedTab({ openedBy: stored })], seq: [['codex', 7]],
    });
    env.respond([chat({ opened_by: authoritative })]);
    const store = env.reload();
    const [existing] = store.getSessions('codex');
    assert.equal(await store.syncServerSessions('codex'), true);
    assert.equal(store.getSession(existing.id), existing);
    assert.deepEqual(existing, { ...savedTab({ openedBy: authoritative }), alive: true });
    assert.equal(env.saved.rows[0].openedBy, authoritative);
    assert.equal(await store.syncServerSessions('codex'), false);
    assert.equal(env.writes, 1);
  });
}

test('origin reconciliation neither revives an ended tab nor adds a dead server session', async t => {
  const env = environment(t, { rows: [savedTab()], seq: [['codex', 7]] });
  const store = env.reload();
  const [existing] = store.getSessions('codex');
  existing.alive = false;
  env.respond([chat({ alive: false }), chat({ session_id: 'console-dead-unseen', alive: false })]);

  assert.equal(await store.syncServerSessions('codex'), true);
  assert.equal(store.getSession(existing.id), existing);
  assert.deepEqual(existing, { ...savedTab({ openedBy: 'orchestrator' }), alive: false });
  assert.deepEqual(store.getSessions('codex'), [existing]);
  assert.deepEqual(env.saved.seq, [['codex', 7]]);
});

test('a failed registry read cannot overwrite a saved origin or replay an operation', async t => {
  const saved = { rows: [savedTab({ openedBy: 'user' })], seq: [['codex', 7]] };
  const env = environment(t, saved);
  env.respond([chat()], 503);
  const store = env.reload();
  const [existing] = store.getSessions('codex');

  assert.equal(await store.syncServerSessions('codex'), false);
  assert.equal(existing.openedBy, 'user');
  assert.deepEqual(env.saved, saved);
  assert.equal(env.writes, 0);
  assert.equal(env.calls.length, 1);
  env.respond([chat()]);
  assert.equal(await store.syncServerSessions('codex'), true);
  assert.equal(existing.openedBy, 'orchestrator');
});

test('disabled storage still allows the live registry to identify Chat tabs', async t => {
  const env = environment(t);
  t.mock.method(env.storage, 'getItem', () => { throw new Error('Storage disabled'); });
  t.mock.method(env.storage, 'setItem', () => { throw new Error('Storage disabled'); });
  env.respond([chat()]);
  const store = env.reload();

  assert.deepEqual(store.getSessions('codex'), []);
  assert.equal(await store.syncServerSessions('codex'), true);
  assert.equal(store.getSession('console-codex-chat').openedBy, 'orchestrator');
  assert.equal(await store.syncServerSessions('codex'), false);
  assert.equal(env.writes, 0);
});
