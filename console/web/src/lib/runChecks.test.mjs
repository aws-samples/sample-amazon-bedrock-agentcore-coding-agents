import assert from 'node:assert/strict';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test, { after } from 'node:test';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';

// Render the real Checks table and Cloudscape controls. The existing Vite
// toolchain compiles TSX; no mock component or browser service supplies the text.
const web = fileURLToPath(new URL('../../', import.meta.url));
const directory = await mkdtemp(path.join(tmpdir(), 'run-checks-test-'));
after(() => rm(directory, { recursive: true, force: true }));
const bundled = await build({
  stdin: {
    contents: `
      import React from 'react';
      import { renderToStaticMarkup } from 'react-dom/server';
      import { Checks } from './src/components/RunDetailPanel';
      export const renderChecks = gates => renderToStaticMarkup(<Checks gates={gates} />);
    `,
    resolveDir: web, loader: 'tsx',
  },
  tsconfig: path.join(web, 'tsconfig.app.json'),
  bundle: true, platform: 'node', format: 'cjs', write: false,
  loader: { '.css': 'empty', '.svg': 'dataurl', '.png': 'dataurl' },
  logLevel: 'silent',
});
const renderer = path.join(directory, 'checks.cjs');
await writeFile(renderer, bundled.outputFiles[0].contents);
const { renderChecks } = createRequire(import.meta.url)(renderer);

const gate = (overrides = {}) => ({
  sequence: 1, stage: 'round 1', passed: false,
  summary: '67 checks, 1 failure', checks: [], ...overrides,
});

test('Checks quotes the early failure in a collapsed section and remains red', () => {
  const record = gate({
    failure_lines: ['  FAIL early probe: <value> & result absent  ', 'not ok 2 - value changed'],
  });
  const before = structuredClone(record);
  const html = renderChecks([record]);
  assert.match(html, /67 checks, 1 failure/);
  assert.match(html, />Failed</);
  assert.match(html, /role="button"[^>]*aria-expanded="false"/);
  assert.match(html, /Failure diagnostics/);
  assert.ok(html.includes(
    '<pre class="console-record">  FAIL early probe: &lt;value&gt; &amp; result absent  \nnot ok 2 - value changed</pre>',
  ));
  assert.deepEqual(record, before);
});

test('old and empty records retain their summary without a diagnostic section', () => {
  for (const record of [gate(), gate({ failure_lines: [] })]) {
    const html = renderChecks([record]);
    assert.match(html, /67 checks, 1 failure/);
    assert.match(html, />Failed</);
    assert.doesNotMatch(html, /Failure diagnostics|console-record/);
  }
  assert.match(renderChecks([]), /No executable checks have been recorded/);
});

test('failure words never change a successful recorded result', () => {
  const html = renderChecks([gate({
    passed: true, summary: 'negative cases completed',
    failure_lines: ['FAIL expected negative case'],
  })]);
  assert.match(html, />Passed</);
  assert.doesNotMatch(html, /Failure diagnostics|FAIL expected negative case/);
});

test('each check execution keeps its own recorded failure in order', () => {
  const html = renderChecks([
    gate({ failure_lines: ['FAIL first observed value'] }),
    gate({ sequence: 2, stage: 'round 2', failure_lines: ['FAIL repair still differs'] }),
  ]);
  const first = html.indexOf('<pre class="console-record">FAIL first observed value</pre>');
  const second = html.indexOf('<pre class="console-record">FAIL repair still differs</pre>');
  assert.ok(first >= 0 && second > first);
  assert.equal((html.match(/>Failed</g) ?? []).length, 2);
});
