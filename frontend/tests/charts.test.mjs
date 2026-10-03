import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import ts from 'typescript';

async function moduleUrl(name) {
  const source = await readFile(new URL('../src/' + name + '.ts', import.meta.url), 'utf8');
  let { outputText } = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } });
  for (const dependency of [...outputText.matchAll(/from ['"]\.\/([^'"]+)['"]/g)]) outputText = outputText.replace(dependency[0], 'from ' + JSON.stringify(await moduleUrl(dependency[1])));
  return 'data:text/javascript;base64,' + Buffer.from(outputText).toString('base64');
}
const { buildChartOption } = await import(await moduleUrl('chartOptions'));
const { palette, chartPalette } = await import(await moduleUrl('theme'));
const { numericValue, formatCell } = await import(await moduleUrl('format'));
const { fullSavedResult, needsFullResult } = await import(await moduleUrl('resultData'));
const query = { id: 'q', columns: ['month', 'sales', 'margin'], rows: [['01', 120, .3], ['02', null, .4], ['03', 170, null]] };
const chart = (type, extras = {}) => ({ type, title: '测试结果', query_id: 'q', x: 'month', y: ['sales'], ...extras });

test('shared professional blue palette preserves values and waterfall sign meaning', () => {
  const before = structuredClone(query);
  const { option } = buildChartOption(chart('line'), query);
  assert.equal(palette.primary, '#1664FF');
  assert.deepEqual(option.color, chartPalette);
  assert.equal(option.textStyle.color, palette.secondary);
  assert.deepEqual(option.series[0].data, [120, null, 170]);
  assert.deepEqual(query, before);
  const { option: waterfall } = buildChartOption(chart('waterfall'), { ...query, rows: [['增加', 120], ['减少', -20]] });
  assert.equal(waterfall.series[1].itemStyle.color, palette.primary);
  assert.equal(waterfall.series[2].itemStyle.color, '#ec8c7d');
  assert.deepEqual(waterfall.series[2].data, [0, 20]);
});

test('missing, blank, nonfinite and booleans are never fabricated as numeric zero', () => {
  for (const value of [null, undefined, '', ' ', false, NaN, Infinity]) assert.equal(numericValue(value), null);
  assert.equal(numericValue(0), 0); assert.equal(numericValue('0'), 0);
  assert.equal(formatCell(null), '—'); assert.equal(formatCell(12345.67), '12,345.67');
});
test('horizontal bar has numeric x and category y preserving query order', () => {
  const { option } = buildChartOption(chart('horizontal_bar'), query);
  assert.equal(option.xAxis.type, 'value'); assert.equal(option.yAxis.type, 'category');
  assert.deepEqual(option.series[0].data, [120, null, 170]);
});
test('area preserves null gaps and does not connect missing observations', () => {
  const { option } = buildChartOption(chart('area'), query);
  assert.equal(option.series[0].type, 'line'); assert.equal(option.series[0].connectNulls, false);
  assert.deepEqual(option.series[0].data, [120, null, 170]); assert.ok(option.series[0].areaStyle);
});
test('long horizontal ranking retains all categories and values with a fifteen-item viewport', () => {
  const q = { id: 'q', columns: ['month', 'sales'], rows: Array.from({ length: 120 }, (_, i) => ['商品' + (i + 1), 120 - i]) };
  const { option } = buildChartOption(chart('horizontal_bar'), q);
  assert.equal(option.yAxis.data.length, 120); assert.equal(option.series[0].data.length, 120);
  assert.deepEqual(option.series[0].data, q.rows.map(row => row[1]));
  assert.equal(option.dataZoom[0].startValue, 0); assert.equal(option.dataZoom[0].endValue, 14);
  assert.equal(option.dataZoom[1].moveOnMouseWheel, true);
  assert.equal(buildChartOption(chart('horizontal_bar'), query).option.dataZoom, undefined);
});
test('stacked bar long-table groups pivot only unique pairs without filling gaps', () => {
  const q = { id: 'q', columns: ['month', 'channel', 'sales'], rows: [['01', '渠道 A', 10], ['02', '渠道 A', 12], ['01', '渠道 B', 5]] };
  const { option } = buildChartOption(chart('stacked_bar', { group_by: 'channel' }), q);
  assert.deepEqual(option.series.map(item => item.name), ['渠道 A', '渠道 B']);
  assert.deepEqual(option.series[1].data, [5, null]); assert.equal(option.series[0].stack, 'total');
  assert.match(buildChartOption(chart('stacked_bar', { group_by: 'channel' }), { ...q, rows: [...q.rows, q.rows[0]] }).warning, /未擅自聚合/);
});
test('combo uses explicit series types, names, units and right axis without ratio scaling', () => {
  const { option } = buildChartOption(chart('combo', { series: [{ column: 'sales', name: '收入', type: 'bar', unit: '元' }, { column: 'margin', name: '毛利率', type: 'line', unit: '比例', axis: 'right' }] }), query);
  assert.equal(option.series[0].name, '收入'); assert.equal(option.series[1].yAxisIndex, 1);
  assert.equal(option.yAxis[0].name, '元'); assert.equal(option.yAxis[1].name, '比例');
  assert.deepEqual(option.series[1].data, [.3, .4, null]);
});
test('units alone never divide monetary values; only explicit value_divisor scales', () => {
  const q = { ...query, rows: [['01', 25000, .3]] };
  assert.equal(buildChartOption(chart('bar', { unit: '万元' }), q).option.series[0].data[0], 25000);
  assert.equal(buildChartOption(chart('bar', { unit: '万元', value_divisor: 10000 }), q).option.series[0].data[0], 2.5);
});
test('mixed units on one value axis safely fall back to the saved table', () => {
  assert.match(buildChartOption(chart('combo', { series: [{ column: 'sales', unit: '元' }, { column: 'margin', unit: '%' }] }), query).warning, /不同单位/);
});
test('scatter keeps business-object names and omits missing rather than inventing points', () => {
  const q = { id: 'q', columns: ['spend', 'roi', 'channel', 'orders'], rows: [[100, 1.2, '搜索广告', 20], [null, 2, '直播', 10], [80, null, '信息流', 5]] };
  const { option } = buildChartOption(chart('scatter', { x: 'spend', x_name: '投放金额', x_unit: '元', y: ['roi'], group_by: 'channel', size: 'orders' }), q);
  assert.deepEqual(option.series[0].data, [{ name: '搜索广告', value: [100, 1.2, 20] }]);
  assert.equal(option.xAxis.name, '投放金额 · 元'); assert.equal(option.series[0].symbolSize([100, 1.2, 20]), 34);
});
test('pie and donut have distinct radius; non-additive measures are not composition', () => {
  const q = { id: 'q', columns: ['channel', 'sales'], rows: [['A', 10], ['B', 5]] };
  assert.equal(buildChartOption(chart('pie', { x: 'channel' }), q).option.series[0].radius[0], '0%');
  assert.equal(buildChartOption(chart('donut', { x: 'channel' }), q).option.series[0].radius[0], '40%');
  assert.match(buildChartOption(chart('pie', { x: 'channel' }), { ...q, non_additive_columns: ['sales'] }).warning, /非可加/);
});
test('pie rejects missing, negative and duplicate-category data', () => {
  assert.match(buildChartOption(chart('pie'), query).warning, /缺失/);
  assert.match(buildChartOption(chart('pie'), { ...query, rows: [['01', -5, .1]] }).warning, /负值/);
  assert.match(buildChartOption(chart('pie'), { ...query, rows: [['01', 5, .1], ['01', 6, .2]] }).warning, /未擅自聚合/);
});
test('truncated charts never extrapolate an incomplete full result', () => {
  assert.match(buildChartOption(chart('line'), { ...query, truncated: true }).warning, /截断/);
});
test('single-measure visualizations do not silently drop extra metrics', () => {
  for (const type of ['pie', 'donut', 'funnel', 'waterfall', 'heatmap']) assert.match(buildChartOption(chart(type, { y: ['sales', 'margin'] }), query).warning, /未擅自丢弃/);
});
test('full chart data is fetched from saved rows once and cached without SQL or task execution', async () => {
  const prior = globalThis.fetch, calls = [];
  const q = { ...query, rows: query.rows.slice(0, 1), total_rows: 3, result_ref: { run_id: 'cached-real', query_id: 'q' } };
  globalThis.fetch = async (url, init) => { calls.push({ url, init }); return new Response(JSON.stringify({ columns: query.columns, rows: query.rows, total: 3, offset: 0, limit: 20000, truncated: false })); };
  try {
    assert.equal(needsFullResult(q), true);
    const [a, b] = await Promise.all([fullSavedResult(q), fullSavedResult(q)]);
    assert.equal(a.rows.length, 3); assert.equal(b.rows.length, 3); assert.equal(calls.length, 1);
    assert.match(calls[0].url, /\/runs\/cached-real\/queries\/q\/rows\?offset=0&limit=20000/); assert.equal(calls[0].init.method, undefined);
  } finally { globalThis.fetch = prior; }
});
test('incomplete full-result response is rejected instead of charting preview as complete', async () => {
  const prior = globalThis.fetch;
  globalThis.fetch = async () => new Response(JSON.stringify({ columns: query.columns, rows: query.rows.slice(0, 1), total: 3, truncated: false }));
  try { await assert.rejects(fullSavedResult({ ...query, rows: query.rows.slice(0, 1), total_rows: 3, result_ref: { run_id: 'incomplete', query_id: 'q' } }), /完整/); }
  finally { globalThis.fetch = prior; }
});
