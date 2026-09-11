import assert from 'node:assert/strict';
import test from 'node:test';
import { createTerminalResizeQueue } from './terminalResize.ts';

const tick = () => new Promise(resolve => setImmediate(resolve));
const fail = error => assert.fail(error.message);

test('a slow resize finishes before the newest size, skipping intermediate geometry', async () => {
  const calls = [];
  let release;
  const queue = createTerminalResizeQueue(async size => {
    calls.push(size);
    if (calls.length === 1) await new Promise(resolve => { release = resolve; });
  }, fail);
  queue.resize({ cols: 80, rows: 24 });
  queue.resize({ cols: 140, rows: 28 });
  queue.resize({ cols: 180, rows: 42 });
  assert.deepEqual(calls, [{ cols: 80, rows: 24 }]);
  release();
  await tick();
  assert.deepEqual(calls, [{ cols: 80, rows: 24 }, { cols: 180, rows: 42 }]);
});

test('repeated fits do not resend an already applied size', async () => {
  const calls = [];
  const queue = createTerminalResizeQueue(async size => { calls.push(size); }, fail);
  queue.resize({ cols: 140, rows: 28 });
  queue.resize({ cols: 140, rows: 28 });
  await tick();
  queue.resize({ cols: 140, rows: 28 });
  await tick();
  assert.equal(calls.length, 1);
});

test('a stalled terminal does not prevent another terminal from resizing', async () => {
  let release;
  const first = createTerminalResizeQueue(() => new Promise(resolve => { release = resolve; }), fail);
  const calls = [];
  const second = createTerminalResizeQueue(async size => { calls.push(size); }, fail);
  first.resize({ cols: 80, rows: 24 });
  second.resize({ cols: 180, rows: 42 });
  await tick();
  assert.deepEqual(calls, [{ cols: 180, rows: 42 }]);
  release();
  await tick();
});

test('closing a terminal discards its pending resize', async () => {
  const calls = [];
  let release;
  const queue = createTerminalResizeQueue(async size => {
    calls.push(size);
    await new Promise(resolve => { release = resolve; });
  }, fail);
  queue.resize({ cols: 80, rows: 24 });
  queue.resize({ cols: 180, rows: 42 });
  queue.stop();
  queue.resize({ cols: 200, rows: 50 });
  release();
  await tick();
  assert.deepEqual(calls, [{ cols: 80, rows: 24 }]);
});

test('a failed resize is reported and a later explicit fit can retry', async () => {
  const calls = [];
  const errors = [];
  const queue = createTerminalResizeQueue(async size => {
    calls.push(size);
    if (calls.length === 1) throw new Error('connection interrupted');
  }, error => errors.push(error.message));
  queue.resize({ cols: 140, rows: 28 });
  await tick();
  assert.equal(calls.length, 1, 'do not create an unbounded retry loop');
  assert.deepEqual(errors, ['connection interrupted']);
  queue.resize({ cols: 140, rows: 28 });
  await tick();
  assert.equal(calls.length, 2);
});
