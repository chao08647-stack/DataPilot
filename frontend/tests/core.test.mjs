import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import ts from 'typescript';

// Transpile the actual dependency-free modules; no browser or synthetic AI service.
async function loadModule(name) {
  const source = await readFile(new URL(`../src/${name}.ts`, import.meta.url), 'utf8');
  const { outputText } = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } });
  return import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`);
}
const { api, ApiError, cell, errorText, printable } = await loadModule('api');
const { mergeRunEvents, lastEventCursor } = await loadModule('events');
const { sourceDisplayName, domainDisplayDescription } = await loadModule('displayLabels');

test('product labels omit demo wording without changing source provenance or custom names', () => {
  const source = { name: '电商经营 · 合成数据', builtin: true };
  assert.equal(sourceDisplayName(source), '电商经营');
  assert.equal(source.name, '电商经营 · 合成数据');
  assert.equal(source.builtin, true);
  assert.equal(sourceDisplayName({ ...source, builtin: false }), source.name);
  const domain = { synthetic: true, table_count: 14, metrics: [{ id: 'sales' }], description: '仅合成数据' };
  assert.equal(domainDisplayDescription(domain), '覆盖 14 张数据表，已配置 1 个指标。');
  assert.equal(domain.description, '仅合成数据');
  assert.equal(domainDisplayDescription({ ...domain, synthetic: false }), '仅合成数据');
});

test('chart/table cells support columns + array rows without treating index as field', () => {
  const query = { columns: ['month', 'revenue'], rows: [['2025-01', 240]] };
  assert.equal(cell(query, query.rows[0], 'month'), '2025-01');
  assert.equal(cell(query, query.rows[0], 'revenue'), 240);
  assert.equal(cell(query, { month: '2025-01', revenue: 240 }, 'revenue'), 240);
  assert.equal(printable(null), '—');
});
test('reconnection IDs normalize, deduplicate and remain ordered', () => {
  const event = id => ({ event_id: id, run_id: 'run-a', type: 'node.started', payload: {}, created_at: '' });
  const events = mergeRunEvents([event(2), event(1)], [event('2'), event(3), event('bad'), event(-1)]);
  assert.deepEqual(events.map(item => item.event_id), [1, 2, 3]);
  assert.equal(lastEventCursor(events), 3);
  assert.equal(lastEventCursor([]), 0);
});
test('history restore is a GET and never transparently retries or posts a new run', async () => {
  const prior = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, options) => { calls.push({ url, options }); return new Response(JSON.stringify({ run_id: 'existing', status: 'waiting_for_input' }), { status: 200 }); };
  try {
    const result = await api('/runs/existing');
    assert.equal(result.run_id, 'existing');
    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, '/api/v1/runs/existing');
    assert.equal(calls[0].options.method, undefined);
    assert.equal(calls[0].options.body, undefined);
  } finally { globalThis.fetch = prior; }
});
test('unconfigured 503 is explicit; fetch does not create fake result or retry', async () => {
  const prior = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = async () => { calls += 1; return new Response(JSON.stringify({ detail: 'PostgreSQL is not configured' }), { status: 503 }); };
  try {
    await assert.rejects(api('/memories?scenario_id=saas'), error => error instanceof ApiError && error.status === 503 && errorText(error).includes('不会生成替代结果'));
    assert.equal(calls, 1);
  } finally { globalThis.fetch = prior; }
});
test('DELETE accepts empty 204 without a JSON parsing failure', async () => {
  const prior = globalThis.fetch;
  globalThis.fetch = async () => new Response(null, { status: 204 });
  try { assert.equal(await api('/memories/example', { method: 'DELETE' }), undefined); }
  finally { globalThis.fetch = prior; }
});
