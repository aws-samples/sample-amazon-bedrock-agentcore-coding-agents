import assert from 'node:assert/strict';
import test from 'node:test';
import { createTerminalInputQueue } from './terminalInput.ts';

const tick = () => new Promise(resolve => setImmediate(resolve));
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

test('a slow paste cannot be overtaken by typing or Enter', async () => {
  const paste = deferred();
  const received = [];
  const queue = createTerminalInputQueue(async input => {
    if (!received.length) await paste.promise;
    received.push(input);
  }, assert.fail);
  queue.send('export A=1\nexport B=2');
  queue.send(' && ');
  queue.send('printf done');
  queue.send('\r');
  await tick();
  assert.deepEqual(received, []);
  paste.resolve();
  await tick();
  assert.equal(received.join(''), 'export A=1\nexport B=2 && printf done\r');
  assert.equal(received.length, 2, 'queued keystrokes are batched, not one HTTP call each');
});

test('one stalled terminal does not block another terminal', async () => {
  const stalled = deferred();
  const received = [];
  const first = createTerminalInputQueue(() => stalled.promise, assert.fail);
  const second = createTerminalInputQueue(async input => received.push(input), assert.fail);
  first.send('long paste');
  second.send('help\r');
  await tick();
  assert.deepEqual(received, ['help\r']);
  stalled.resolve();
});

test('an uncertain send failure is reported once and never replayed', async () => {
  const request = deferred();
  const sent = [];
  const errors = [];
  const queue = createTerminalInputQueue(input => {
    sent.push(input);
    return request.promise;
  }, error => errors.push(error.message));
  queue.send('first command\r');
  queue.send('second command\r');
  request.reject(new Error('connection lost'));
  await tick();
  queue.send('third command\r');
  await tick();
  assert.deepEqual(sent, ['first command\r']);
  assert.deepEqual(errors, ['connection lost']);
});

test('closing a terminal discards input that has not been sent', async () => {
  const request = deferred();
  const sent = [];
  const queue = createTerminalInputQueue(input => {
    sent.push(input);
    return request.promise;
  }, assert.fail);
  queue.send('first');
  queue.send('queued');
  queue.stop();
  request.resolve();
  await tick();
  queue.send('after close');
  assert.deepEqual(sent, ['first']);
});
