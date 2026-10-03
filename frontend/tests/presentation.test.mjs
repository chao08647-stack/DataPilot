import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import ts from 'typescript';

const source = await readFile(new URL('../src/presentation.ts', import.meta.url), 'utf8');
const { outputText } = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } });
const { shortQuestionTitle, eventPresentation, orderedRunEvents, deriveProgress } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`);
const event = (id, type, payload = {}) => ({ event_id: id, run_id: 'run-1', type, payload, created_at: '2026-10-02T08:00:00Z' });

test('short titles only use exact catalog matches and never alter original questions', () => {
  const question = '比较各地区履约成本';
  const catalog = [{ id: 'costs', title: '地区履约成本对比', question, domain_id: 'domain' }];
  assert.equal(shortQuestionTitle(question, catalog), '地区履约成本对比');
  assert.equal(shortQuestionTitle(`${question} `, catalog), `${question} `);
  assert.equal(shortQuestionTitle('短问题', catalog), '短问题');
  const long = '😀'.repeat(24) + '补充内容';
  assert.equal(shortQuestionTitle(long, catalog), '😀'.repeat(24) + '…');
  assert.equal(shortQuestionTitle('问'.repeat(24), []), '问'.repeat(24));
  assert.equal(catalog[0].question, question);
});

test('event summaries are bounded, Chinese-labelled, and never fall back to raw objects', () => {
  const long = eventPresentation(event(1, 'run.input_required', { question: '😀'.repeat(100) }));
  assert.equal(Array.from(long.summary).length, 80);
  assert.equal(long.label, '等待补充信息');
  const unknown = eventPresentation(event(2, 'future.event', { message: 'do not invent this', secret: ['large data'] }));
  assert.equal(unknown.label, '其他事件');
  assert.equal(unknown.summary, '已记录此事件，展开查看原始数据。');
  assert.equal(eventPresentation(event(8, 'constructor')).label, '其他事件');
  assert.equal(eventPresentation(event(3, 'artifact.created', { artifact: 'queries', value: { sql: 'SELECT 1' } })).summary, '查询结果已保存。');
  assert.equal(eventPresentation(event(4, 'node.started', { node: 'repair' })).summary, '修复查询');
  assert.doesNotMatch(eventPresentation(event(5, 'node.completed', { node: { nested: 'raw' } })).summary, /raw|nested|\{/);
  assert.match(eventPresentation(event(6, 'memory.recalled', { ids: ['a', 'b'] })).summary, /2 条.*是否应用/);
});

test('presentation orders and deduplicates records without mutating SSE input', () => {
  const original = [event(3, 'node.started'), event(1, 'run.started'), event(3, 'node.completed')];
  assert.deepEqual(orderedRunEvents(original).map(item => item.event_id), [1, 3]);
  assert.equal(orderedRunEvents(original).at(-1).type, 'node.completed');
  assert.deepEqual(original.map(item => item.event_id), [3, 1, 3]);
});

test('missing phase history never fabricates completed stages', () => {
  for (const status of ['running', 'completed', 'waiting_for_input', 'failed', 'cancelled', 'interrupted']) {
    assert.deepEqual(deriveProgress([event(1, 'run.completed')], status).stages, []);
  }
  const onlySql = deriveProgress([event(1, 'node.completed', { node: 'execute' })], 'completed');
  assert.deepEqual(onlySql.stages.map(stage => stage.state), ['pending', 'complete', 'pending', 'pending']);
  assert.deepEqual(deriveProgress([event(1, 'node.started', { node: 'future_node' })], 'running').stages, []);
  assert.deepEqual(deriveProgress([event(1, 'node.started', { node: 'constructor' })], 'running').stages, []);
});

test('repair and supplemental queries return to the actual phase, clearing stale downstream state', () => {
  const history = [event(1, 'node.completed', { node: 'discovery' }), event(2, 'node.completed', { node: 'execute' }), event(3, 'node.completed', { node: 'analysis' }), event(4, 'node.started', { node: 'supplement' }), event(5, 'node.started', { node: 'sql' })];
  assert.deepEqual(deriveProgress(history, 'running').stages.map(stage => stage.state), ['complete', 'active', 'pending', 'pending']);
  const repair = [...history, event(6, 'node.started', { node: 'repair' })];
  assert.equal(deriveProgress(repair, 'running').stages[1].state, 'active');
  assert.equal(deriveProgress([event(2, 'node.started', { node: 'sql' })], 'running').stages[0].state, 'pending');
});

test('waiting, failure, cancellation, interruption and partial results are distinct states', () => {
  const history = [event(1, 'node.started', { node: 'clarify' })];
  assert.equal(deriveProgress(history, 'waiting_for_input').stages[0].state, 'paused');
  assert.equal(deriveProgress(history, 'failed').label, '分析失败');
  assert.equal(deriveProgress(history, 'failed').stages[0].state, 'failed');
  assert.equal(deriveProgress(history, 'cancelled').stages[0].state, 'cancelled');
  assert.equal(deriveProgress(history, 'interrupted').label, '已中断');
  assert.equal(deriveProgress(history, 'completed', true).tone, 'partial');
  assert.match(deriveProgress(history, 'failed', true).label, /已有部分结果/);
  assert.equal(deriveProgress([event(1, 'node.started', { node: 'execute' })], 'completed').stages[1].state, 'pending');
});
