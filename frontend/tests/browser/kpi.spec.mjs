import { test, expect } from '@playwright/test';
import { enterpriseApi } from './fixtures.mjs';

const components = ['gross_sales', 'discounts', 'refunds', 'cogs', 'fulfillment', 'payment_fees', 'marketing'];
function groupedQuery() {
  return { id: 'grouped', sql: 'SELECT month, channel, components FROM approved_result', columns: ['month', 'channel', ...components], rows: ['2025-10', '2025-11'].flatMap((month, monthIndex) => ['自然流量', '付费推广', '平台商城'].map((channel, index) => [month, channel, ...components.map((_, component) => 10000 * (monthIndex + 1) + 1000 * index + component)])) };
}
function kpi(queryId, y = components) { return { type: 'kpi', title: '贡献利润', query_id: queryId, y, unit: '万元', value_divisor: 10000 }; }

test('historic grouped analysis KPI preserves all six rows and seven components as query results', async ({ page }) => {
  const { state, requests } = await enterpriseApi(page);
  const query = groupedQuery();
  state.run.artifacts.queries = [query]; state.run.artifacts.charts = [kpi(query.id)];
  await page.goto('/#analysis'); await page.locator('.recent-run').first().click();
  await expect(page.getByRole('region', { name: '查询结果', exact: true })).toBeVisible();
  await expect(page.locator('.chart-data-warning')).toContainText('查询返回 6 行');
  await expect(page.locator('.chart-data-warning')).toContainText('不取首行、不求和');
  await expect(page.locator('.kpi')).toHaveCount(0);
  await expect(page.locator('.chart-card tbody tr')).toHaveCount(6);
  await expect(page.locator('.chart-card thead th')).toHaveCount(9);
  await expect(page.locator('.chart-card tbody tr').first()).toContainText('10,000');
  await expect(page.locator('.chart-card tbody tr').last()).toContainText('22,006');
  // Currency display preferences must not rescale the source table during fallback.
  expect(requests.filter(request => request.method !== 'GET')).toEqual([]);
});

test('historic analysis KPI also degrades rather than silently selecting the first row', async ({ page }) => {
  const { state, requests } = await enterpriseApi(page);
  const query = groupedQuery();
  state.run.artifacts.queries = [query]; state.run.artifacts.charts = [kpi(query.id)];
  await page.goto('/#analysis');
  await page.locator('.recent-run').first().click();
  await expect(page.locator('.chart-card .chart-data-warning')).toContainText('原图表标题不代表已完成指标汇总');
  await expect(page.locator('.chart-card tbody tr')).toHaveCount(6);
  await expect(page.locator('.kpi')).toHaveCount(0);
  expect(requests.filter(request => request.method !== 'GET')).toEqual([]);
});

for (const objectRows of [false, true]) {
  test(`a complete single-row KPI remains valid (${objectRows ? 'object' : 'array'} rows)`, async ({ page }) => {
    const { state } = await enterpriseApi(page);
    const query = { id: 'total', sql: 'SELECT SUM(net) AS net FROM approved_result', columns: ['net'], rows: objectRows ? [{ net: 25000 }] : [[25000]] };
    state.run.artifacts.queries = [query]; state.run.artifacts.charts = [kpi(query.id, ['net'])];
    await page.goto('/#analysis');
    await page.locator('.recent-run').first().click();
    await expect(page.locator('.chart-card .kpi strong')).toHaveText('2.5万元');
    await expect(page.locator('.chart-card .chart-data-warning')).toHaveCount(0);
  });
}

test('a truncated one-row result is not treated as a complete KPI', async ({ page }) => {
  const { state } = await enterpriseApi(page);
  const query = { id: 'limited', sql: 'SELECT net FROM approved_result LIMIT 1', columns: ['net'], rows: [[120]], truncated: true };
  state.run.artifacts.queries = [query]; state.run.artifacts.charts = [kpi(query.id, ['net'])];
  await page.goto('/#analysis');
  await page.locator('.recent-run').first().click();
  await expect(page.locator('.chart-data-warning')).toContainText('查询结果已截断');
  await expect(page.locator('.kpi')).toHaveCount(0);
  await expect(page.locator('.chart-card tbody tr')).toHaveCount(1);
});

test('empty KPI result stays empty and does not invent a zero', async ({ page }) => {
  const { state } = await enterpriseApi(page);
  state.run.artifacts.queries = [{ id: 'empty', columns: ['net'], rows: [] }]; state.run.artifacts.charts = [kpi('empty', ['net'])];
  await page.goto('/#analysis');
  await page.locator('.recent-run').first().click();
  await expect(page.locator('.chart-card')).toContainText('没有符合当前条件的数据');
  await expect(page.locator('.kpi')).toHaveCount(0);
});

test('single-row KPI displays each series name and unit instead of the global unit', async ({ page }) => {
  const { state, requests } = await enterpriseApi(page);
  state.run.artifacts.queries = [{ id: 'mixed-kpi', columns: ['amount', 'orders', 'aov'], rows: [[25000, 100, 250]] }];
  state.run.artifacts.charts = [{ type: 'kpi', title: '订单概览', query_id: 'mixed-kpi', y: ['amount', 'orders', 'aov'], unit: '不应使用的全局单位', series: [{ column: 'amount', name: '实付金额', unit: '元' }, { column: 'orders', name: '完成订单', unit: '单' }, { column: 'aov', name: '客单价', unit: '元/单' }] }];
  await page.goto('/#analysis'); await page.locator('.recent-run').first().click();
  await expect(page.locator('.chart-card .kpi>span')).toHaveText(['实付金额', '完成订单', '客单价']);
  await expect(page.locator('.chart-card .kpi strong')).toHaveText(['25,000元', '100单', '250元/单']);
  await expect(page.locator('.chart-data-warning')).toHaveCount(0);
  expect(requests.filter(request => request.method !== 'GET')).toEqual([]);
});
