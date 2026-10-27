import assert from 'node:assert/strict';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test, { after } from 'node:test';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';

// Render the real evidence views and Cloudscape controls. The existing Vite
// toolchain compiles TSX; no mock component or browser service supplies the text.
const web = fileURLToPath(new URL('../../', import.meta.url));
const directory = await mkdtemp(path.join(tmpdir(), 'run-checks-test-'));
after(() => rm(directory, { recursive: true, force: true }));
const bundled = await build({
  stdin: {
    contents: `
      import React from 'react';
      import { renderToStaticMarkup } from 'react-dom/server';
      import { Checks, Reviews } from './src/components/RunDetailPanel';
      import { MarkdownContent } from './src/components/MarkdownContent';
      import { BuildSnapshot } from './src/components/BuildSnapshot';
      export const renderChecks = gates => renderToStaticMarkup(<Checks gates={gates} />);
      export const renderReviews = review => renderToStaticMarkup(<Reviews review={review} />);
      export const renderMarkdown = text => renderToStaticMarkup(<MarkdownContent text={text} />);
      export const renderSnapshot = snapshot => renderToStaticMarkup(<BuildSnapshot snapshot={snapshot} />);
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
const { renderChecks, renderReviews, renderMarkdown, renderSnapshot } = createRequire(import.meta.url)(renderer);

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

test('one or several recorded details preserve their notes and outcomes', () => {
  const checks = [
    { check: 'timer stays paused', passed: true },
    { check: 'saved score survives reload', passed: false },
  ];
  for (const count of [1, 2]) {
    const record = gate({ checks: checks.slice(0, count) });
    const before = structuredClone(record);
    const html = renderChecks([record]);
    assert.ok(html.includes(`${count} recorded detail${count === 1 ? '' : 's'}`));
    assert.match(html, />Failed</); // The gate result remains independent of its entries.
    assert.match(html, /timer stays paused/);
    if (count === 2) assert.match(html, /saved score survives reload/);
    else assert.doesNotMatch(html, /1 recorded details|saved score survives reload/);
    assert.deepEqual(record, before);
  }

  const record = gate({
    passed: true,
    summary: 'NOTE: run in jsdom with real shipped bundle; real-browser rendering/OS IME delivery not verified.',
    checks: [{
      check: 'acceptance_check_authored', passed: true,
      detail: "the validator's authored check passed against the live deliverable",
    }],
  });
  const before = structuredClone(record);
  const html = renderChecks([record]);
  assert.match(html, /1 recorded detail/);
  assert.match(html, /NOTE: run in jsdom with real shipped bundle; real-browser rendering\/OS IME delivery not verified\./);
  assert.match(html, /acceptance_check_authored/);
  assert.match(html, />Passed</);
  assert.doesNotMatch(html, /recorded assertion|>Failed</);
  assert.deepEqual(record, before);
});

test('review panels format Markdown without changing the verdict or its evidence limits', () => {
  const record = {
    state: 'changes_requested', round: 2, reasons: ['Verify the input method in a browser.'],
    panels: [{
      name: 'integrated', label: 'Integrated review', state: 'changes_requested',
      assessment: '#### Adversarial verification\n\n**Executed:** the served bundle in jsdom.\n\n'
        + '**Not verified:** real-browser and OS input-method event delivery.\n\n'
        + '- Preserve `KeyboardEvent.code`.\n- Keep saved scores.',
      lenses: {
        adversarial: '**Scope:** jsdom only.',
        design: 'Keep the existing `togglePause` interface.',
      },
    }],
  };
  const before = structuredClone(record);
  const html = renderReviews(record);
  assert.match(html, /<h4>Adversarial verification<\/h4>/);
  assert.match(html, /<strong>Executed:<\/strong> the served bundle in jsdom/);
  assert.match(html, /<strong>Not verified:<\/strong> real-browser and OS input-method event delivery/);
  assert.match(html, /<li>Preserve <code>KeyboardEvent\.code<\/code>\.<\/li>/);
  assert.match(html, /<strong>Scope:<\/strong> jsdom only/);
  assert.match(html, /existing <code>togglePause<\/code> interface/);
  assert.equal((html.match(/>Changes requested</g) ?? []).length, 2);
  assert.doesNotMatch(html, />Approved<|#### Adversarial|\*\*Executed/);
  assert.deepEqual(record, before);
});

test('the shared Markdown renderer rejects active HTML and unsafe links but preserves code and GFM', () => {
  const html = renderMarkdown([
    '<script>window.untrusted = true</script>',
    '<img src="x" onerror="window.untrusted = true">',
    '<details><summary>Evidence</summary>',
    '**Preserved evidence**',
    '</details>',
    '[Unsafe](javascript:alert%281%29) and [safe](https://example.com/review)',
    '```text\n<img src="x" onerror="literal example">\n```',
    '| Check | Result |\n| --- | --- |\n| Timer | ~~unverified~~ recorded |',
  ].join('\n\n'));
  assert.doesNotMatch(html, /<script\b|<img\b|<details\b|<summary\b|javascript:/i);
  assert.match(html, /<strong>Preserved evidence<\/strong>/);
  assert.match(html, /href="https:\/\/example\.com\/review"/);
  assert.match(html, /&lt;img src=&quot;x&quot; onerror=&quot;literal example&quot;&gt;/);
  assert.match(html, /<table>/);
  assert.match(html, /<del>unverified<\/del> recorded/);
});

test('legacy review text uses the same renderer and retains an abstained state', () => {
  const record = {
    state: 'abstained', round: 1,
    assessment: '### Review unavailable\n\n**No verdict:** the reviewer did not finish.',
  };
  const before = structuredClone(record);
  const html = renderReviews(record);
  assert.match(html, /<h3>Review unavailable<\/h3>/);
  assert.match(html, /<strong>No verdict:<\/strong> the reviewer did not finish/);
  assert.match(html, />Abstained</);
  assert.doesNotMatch(html, />Approved</);
  assert.deepEqual(record, before);
});

test('the build snapshot shows all recorded files without presenting additions as a PR diff', () => {
  const snapshot = {
    run_id: 'run_snapshot', commit: 'recorded-commit', branch: 'run/run_snapshot',
    files: [
      { path: 'README.md', added: 1, removed: 0, patch: '@@ -0,0 +1 @@\n+Existing instructions' },
      { path: 'src/main.js', added: 1, removed: 0, patch: '@@ -0,0 +1 @@\n+const example = "<script>";' },
      { path: 'image.png', added: null, removed: null, patch: 'Binary files /dev/null and b/image.png differ' },
    ],
  };
  const before = structuredClone(snapshot);
  const html = renderSnapshot(snapshot);
  assert.match(html, /Build snapshot/);
  assert.match(html, /including unchanged files/);
  assert.match(html, /patches start from an empty snapshot/);
  for (const file of snapshot.files) assert.ok(html.includes(file.path));
  assert.match(html, /Existing instructions/);
  assert.match(html, /&lt;script&gt;/);
  assert.doesNotMatch(html, /Changed files|lines added|lines removed|<script>/);
  assert.deepEqual(snapshot, before);
  assert.equal(renderSnapshot({ ...snapshot, files: [] }), '');
});
