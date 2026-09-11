import assert from 'node:assert/strict';
import { setTimeout } from 'node:timers/promises';
import test from 'node:test';
import headless from '@xterm/headless';
import { createTerminalOutputQueue } from './terminalOutput.ts';

const { Terminal } = headless;

function setup(t, options = {}) {
  const terminal = new Terminal({ cols: 80, rows: 12, allowProposedApi: true, ...options });
  const input = [];
  const listener = terminal.onData(data => input.push(data));
  const output = createTerminalOutputQueue(terminal);
  t.after(() => { listener.dispose(); output.stop(); terminal.dispose(); });
  const text = () => Array.from(
    { length: terminal.buffer.active.length },
    (_, line) => terminal.buffer.active.getLine(line)?.translateToString(true) ?? '',
  ).join('\n').trim();
  return { terminal, input, output, text };
}

async function until(predicate) {
  const deadline = Date.now() + 2000;
  while (!predicate()) {
    assert.ok(Date.now() < deadline, 'The real xterm parser did not finish the queued output.');
    await setTimeout(1);
  }
}

test('the real parser answers device queries without replay protection', async t => {
  const { terminal, input } = setup(t);
  await new Promise(resolve => terminal.write('\x1b[c\x1b[6n', resolve));
  assert.equal(input.length, 2, 'Keep the parser behavior behind the live regression explicit.');
});

test('history renders without replying into the current shell', async t => {
  const { input, output, text, terminal } = setup(t);
  output.write('old TUI\x1b[c\x1b[6n\r\nshell ready', true);
  await until(() => text().includes('shell ready'));
  assert.deepEqual(input, []);
  assert.equal(terminal.options.disableStdin, false);
  assert.match(text(), /^old TUI\nshell ready/);
});

test('a live query after queued history still gets exactly its own reply', async t => {
  const { input, output, text } = setup(t);
  output.write('history\x1b[c\x1b[6n', true);
  output.write('\r\nlive\x1b[6n');
  output.write('\r\nready');
  await until(() => text().includes('ready'));
  assert.equal(input.length, 1);
  assert.match(input[0], /^\x1b\[\d+;\d+R$/);
  assert.equal(text(), 'history\nlive\nready');
});

test('reconnect replaces history rather than duplicating it', async t => {
  const { input, output, text } = setup(t);
  output.write('one marker\x1b[c', true);
  output.write('\r\nafter marker');
  output.write('one marker\x1b[c\r\nafter marker', true);
  output.write('\r\nreconnected');
  await until(() => text().includes('reconnected'));
  assert.equal(text(), 'one marker\nafter marker\nreconnected');
  assert.deepEqual(input, []);
});

test('an empty history boundary clears the previous screen', async t => {
  const { output, text } = setup(t);
  output.write('old shell');
  await until(() => text() === 'old shell');
  output.write('', true);
  await until(() => text() === '');
});

test('read-only terminals remain read-only after history finishes', async t => {
  const { terminal, input, output, text } = setup(t, { disableStdin: true });
  output.write('history\x1b[c', true);
  output.write('\r\nlive\x1b[6n\r\nready');
  await until(() => text().includes('ready'));
  assert.equal(terminal.options.disableStdin, true);
  assert.deepEqual(input, []);
});

test('stopping drops pending writes without enabling an in-flight replay', async t => {
  const { input, output, text, terminal } = setup(t);
  output.write('history\x1b[c\r\ncomplete', true);
  output.write('\r\nmust not render\x1b[6n');
  output.stop();
  await until(() => text().includes('complete'));
  assert.deepEqual(input, []);
  assert.equal(terminal.options.disableStdin, false);
  assert.equal(text(), 'history\ncomplete');
});
