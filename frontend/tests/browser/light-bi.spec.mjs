import { test, expect } from '@playwright/test';
import { enterpriseApi, completed } from './fixtures.mjs';

// Browser-only mocks: none of these answers or rows ship with the application.
function richHistory(state) {
  const rows = Array.from({ length: 12 }, (_, i) => ['2025-' + String(i + 1).padStart(2, '0'), [182, 190, 203, 216, 226, 236, 245, 251, 258, 249, 272, 291][i] * 10000, [18.2, 18.4, 19.1, 19.6, 20.1, 20.2, 21, 20.8, 21.3, 20.6, 21.8, 22.4][i]]);
  state.fullRows.trend = rows;
  state.run = {
    ...structuredClone(completed), question: '2025 年收入与利润率如何变化，哪些渠道贡献最大？',
    artifacts: {
      analysis: { summary: '全年收入整体上升，12 月收入达到 291 万元，利润率为 22.4%。直营与自然流量是主要贡献渠道。', findings: [{ title: '收入增长与利润率改善同步', detail: '1 月至 12 月，月收入从 182 万元上升至 291 万元；利润率从 18.2% 上升至 22.4%。该结果描述同期变化，不代表因果关系。', kind: 'fact' }], recommendations: ['结合渠道费用与退款明细继续核对利润变化。'], limitations: ['本页内容仅为浏览器布局测试数据，不会进入应用历史。'] },
      queries: [
        { id: 'trend', title: '月度收入与利润率', sql: 'SELECT month, revenue, margin_pct FROM approved_monthly_results', columns: ['month', 'revenue', 'margin_pct'], rows: rows.slice(0, 4), total_rows: rows.length, result_ref: { run_id: completed.run_id, query_id: 'trend' } },
        { id: 'channels', sql: 'SELECT channel, profit FROM approved_channel_results', columns: ['channel', 'profit'], rows: [['直营', 920000], ['自然流量', 760000], ['搜索广告', 540000], ['合作伙伴', 370000], ['平台商城', 210000]] },
      ],
      charts: [
        { title: '月度收入与利润率', type: 'combo', query_id: 'trend', x: 'month', y: ['revenue', 'margin_pct'], series: [{ column: 'revenue', name: '收入', unit: '元', type: 'bar' }, { column: 'margin_pct', name: '利润率', unit: '%', axis: 'right', type: 'line' }] },
        { title: '渠道利润贡献', type: 'horizontal_bar', query_id: 'channels', x: 'channel', y: ['profit'], series: [{ column: 'profit', name: '利润贡献', unit: '元' }] },
      ],
    },
  };
  state.additionalRuns = Array.from({ length: 9 }, (_, i) => ({
    ...structuredClone(completed), run_id: 'run-history-' + i, thread_id: i < 2 ? completed.thread_id : 'thread-history-' + i,
    domain_id: i === 8 ? 'finance-custom' : 'supply-chain',
    question: ['哪个渠道的退款率持续上升？', '比较第三季度各地区履约时效', '新老客户的复购贡献有什么差异？', '广告投入与利润变化是否一致？', '哪些商品库存周转明显放缓？', '退款原因主要集中在哪些环节？', '本月订单履约成本为什么变化？', '地区维度的净收入排名', '财务核对：收入确认与收款差额'][i],
    created_at: '2026-09-' + String(29 - i).padStart(2, '0') + 'T08:00:00Z',
  }));
}
for (const width of [1440, 1280]) {
  test('rich saved history and rendered charts at ' + width + 'px', async ({ page }, testInfo) => {
    const { state, requests } = await enterpriseApi(page); richHistory(state);
    await page.setViewportSize({ width, height: 1100 }); await page.goto('/');
    await expect(page.locator('.recent-run')).toHaveCount(10);
    await page.locator('.recent-run').first().click();
    await expect(page.locator('.chart-canvas canvas')).toHaveCount(2);
    await expect(page.locator('.chart-loading')).toHaveCount(0);
    await page.evaluate(() => document.fonts.ready);
    // ECharts entrance animation is visual only; wait for its pixels to settle.
    await page.waitForTimeout(900);
    await page.screenshot({ path: testInfo.outputPath('rich-analysis-' + width + '.png'), fullPage: true });
    const visibleFontSizes = await page.locator('.enterprise-shell').evaluate(root => [...root.querySelectorAll('p,span,small,label,button,input,select,textarea,td,th,summary,code,li,dt,dd')].filter(node => node.getBoundingClientRect().width && node.getBoundingClientRect().height).map(node => parseFloat(getComputedStyle(node).fontSize)));
    expect(Math.min(...visibleFontSizes)).toBeGreaterThanOrEqual(12);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
    expect(requests.filter(item => item.method !== 'GET')).toEqual([]);
    expect(requests.some(item => item.path.endsWith('/queries/trend/rows') && item.query.includes('limit=20000'))).toBeTruthy();
  });
}
test('history search and domain filter open a specific older run, not the latest in its thread', async ({ page }) => {
  const { state, requests } = await enterpriseApi(page); richHistory(state);
  await page.goto('/'); await page.getByLabel('搜索分析记录').fill('退款率');
  await expect(page.locator('.recent-run')).toHaveCount(1); await page.locator('.recent-run').click();
  await expect(page.locator('.question-card h1')).toHaveText('哪个渠道的退款率持续上升？');
  expect(requests.filter(item => item.path === '/threads/thread-existing')).toEqual([]);
  await page.reload(); await expect(page.locator('.question-card h1')).toHaveText('哪个渠道的退款率持续上升？');
  await page.getByLabel('搜索分析记录').fill(''); await page.getByLabel('历史记录业务域').selectOption('finance-custom');
  await expect(page.locator('.recent-run')).toHaveCount(1); await page.locator('.recent-run').click();
  await expect(page.getByLabel('当前业务域')).toHaveValue('finance-custom');
  await expect(page.locator('.question-card h1')).toHaveText('财务核对：收入确认与收款差额');
  expect(requests.filter(item => item.method !== 'GET')).toEqual([]);
});
test('completed partial runs remain marked for review in history and after detail restore', async ({ page }) => {
  const { state, requests } = await enterpriseApi(page);
  state.run.artifacts.partial = true;
  state.additionalRuns = [{ ...structuredClone(completed), run_id: 'full-result', question: '已完整核验的分析' }];
  await page.goto('/');
  await expect(page.locator('.recent-run').first().locator('.history-status')).toHaveText('部分结果 · 待复核');
  await expect(page.locator('.recent-run').nth(1).locator('.history-status')).toHaveText('已完成');
  await page.locator('.recent-run').first().click();
  await expect(page.locator('.run-status')).toHaveText('部分结果 · 待复核');
  await expect(page.locator('.run-status')).toHaveClass(/partial/);
  await expect(page.locator('.partial-result-notice')).toContainText('不应视为完整分析成功');
  await expect(page.getByRole('button', { name: /固定.*到看板/ })).toHaveCount(0);
  await expect(page.locator('.answer-summary')).toBeVisible();
  await page.reload();
  await expect(page.locator('.run-status')).toHaveText('部分结果 · 待复核');
  await page.locator('.recent-run').nth(1).click();
  await expect(page.locator('.run-status')).toHaveText('已完成');
  await expect(page.locator('.partial-result-notice')).toHaveCount(0);
  expect(requests.filter(item => item.method !== 'GET')).toEqual([]);
});
test('saved full table pages and sorts via GET, retains nulls and never repeats SQL', async ({ page }) => {
  const { state, requests } = await enterpriseApi(page);
  const rows = Array.from({ length: 63 }, (_, i) => ['客户 ' + String(i + 1).padStart(2, '0'), i === 0 ? null : 10000 + i]);
  state.fullRows.customers = rows;
  state.run.artifacts.queries = [{ id: 'customers', columns: ['customer', 'value'], rows: rows.slice(0, 4), total_rows: 63, result_ref: { run_id: completed.run_id, query_id: 'customers' } }];
  state.run.artifacts.charts = [];
  await page.goto('/'); await page.locator('.recent-run').first().click();
  const table = page.locator('.saved-query');
  await expect(table.locator('tbody tr')).toHaveCount(10);
  await expect(table.locator('tbody tr').first()).toContainText('—');
  await expect(table.locator('.table-pagination')).toContainText('共 63 行');
  await table.getByLabel('下一页 customers').click();
  await expect(table.locator('tbody tr').first()).toContainText('客户 11');
  await table.getByLabel('按 value 排序').click();
  await table.getByLabel('按 value 排序').click();
  await expect(table.locator('tbody tr').first()).toContainText('10,062');
  await table.getByLabel('每页行数 customers').selectOption('25');
  await expect(table.locator('tbody tr')).toHaveCount(25);
  expect(await table.locator('thead th').first().evaluate(node => getComputedStyle(node).position)).toBe('sticky');
  expect(requests.some(item => item.query.includes('sort_by=value') && item.query.includes('descending=true'))).toBeTruthy();
  expect(requests.filter(item => item.method !== 'GET')).toEqual([]);
});
test('MySQL is unavailable for new connections and follow-up uses the selected run thread', async ({ page }) => {
  const { requests } = await enterpriseApi(page);
  await page.goto('/#data'); await page.getByRole('button', { name: '接入数据源', exact: true }).click();
  await expect(page.getByLabel('连接类型').locator('option')).toHaveCount(2);
  await expect(page.getByLabel('连接类型').locator('option[value=mysql]')).toHaveCount(0);
  await page.getByRole('button', { name: '关闭数据源配置' }).click();
  await page.locator('.recent-run').first().click();
  await expect(page.locator('.composer-bottom')).toContainText('供应链与履约');
  await expect(page.locator('.composer-bottom')).not.toContainText('继续本次会话');
  await page.getByLabel('分析问题', { exact: true }).fill('再按地区比较');
  await page.getByRole('button', { name: '发送分析问题' }).click();
  await expect(page.locator('.clarification-card')).toBeVisible();
  expect(requests.find(item => item.path === '/runs' && item.method === 'POST').body.thread_id).toBe('thread-existing');
});

test('scatter tooltip retains business-object labels and series units', async ({ page }, testInfo) => {
  const { state, requests } = await enterpriseApi(page);
  state.run.artifacts.queries = [{ id: 'scatter', columns: ['channel', 'spend', 'roi', 'orders'], rows: [['搜索广告', 120000, 1.8, 260], ['内容推广', 85000, 2.1, 180], ['合作渠道', 180000, 1.3, 340]] }];
  state.run.artifacts.charts = [{ type: 'scatter', title: '渠道投入与回报', query_id: 'scatter', x: 'spend', x_name: '投入', x_unit: '元', group_by: 'channel', size: 'orders', y: ['roi'], series: [{ column: 'roi', name: '投入产出比', unit: '倍' }] }];
  await page.goto('/'); await page.locator('.recent-run').first().click();
  await expect(page.locator('.chart-canvas canvas')).toHaveCount(1);
  const data = await page.evaluate(async () => {
    const url = performance.getEntriesByType('resource').map(item => item.name).find(name => name.includes('/echarts_core.js'));
    const { getInstanceByDom } = await import(url);
    const instance = getInstanceByDom(document.querySelector('.chart-canvas'));
    instance.dispatchAction({ type: 'showTip', seriesIndex: 0, dataIndex: 0 });
    const series = instance.getOption().series[0];
    return { data: series.data, dimensions: series.dimensions };
  });
  expect(data.data[0].name).toBe('搜索广告');
  expect(data.dimensions).toEqual(['投入 · 元', '投入产出比 · 倍', 'orders']);
  await page.waitForTimeout(900);
  await page.locator('.chart-card').screenshot({ path: testInfo.outputPath('scatter-object-tooltip.png') });
  expect(requests.filter(item => item.method !== 'GET')).toEqual([]);
});
test('long saved ranking keeps all 120 products and scrolls the fifteen-item viewport', async ({ page }, testInfo) => {
  const { state, requests } = await enterpriseApi(page);
  const rows = Array.from({ length: 120 }, (_, index) => ['商品' + String(index + 1).padStart(3, '0'), 120000 - index * 500]);
  state.fullRows.products = rows;
  state.run.artifacts.queries = [{ id: 'products', columns: ['product', 'amount'], rows: rows.slice(0, 50), total_rows: 120, result_ref: { run_id: completed.run_id, query_id: 'products' } }];
  state.run.artifacts.charts = [{ type: 'horizontal_bar', title: '全部商品退款金额排名', query_id: 'products', x: 'product', y: ['amount'], series: [{ column: 'amount', name: '退款金额', unit: '元' }] }];
  await page.goto('/'); await page.locator('.recent-run').first().click();
  await expect(page.locator('.chart-canvas canvas')).toHaveCount(1);
  await expect(page.locator('.chart-window-note')).toContainText('共 120 个分类');
  await expect(page.locator('.chart-window-note')).toContainText('仅调整视窗');
  const result = await page.evaluate(async () => {
    const url = performance.getEntriesByType('resource').map(item => item.name).find(name => name.includes('/echarts_core.js'));
    const { getInstanceByDom } = await import(url);
    const instance = getInstanceByDom(document.querySelector('.chart-canvas'));
    const initial = instance.getOption();
    instance.dispatchAction({ type: 'dataZoom', startValue: 105, endValue: 119 });
    const final = instance.getOption();
    return { count: initial.series[0].data.length, first: initial.dataZoom[0].startValue, last: initial.dataZoom[0].endValue, scrolledFirst: final.dataZoom[0].startValue, scrolledLast: final.dataZoom[0].endValue, finalCategory: final.yAxis[0].data.at(-1) };
  });
  expect(result).toEqual({ count: 120, first: 0, last: 14, scrolledFirst: 105, scrolledLast: 119, finalCategory: '商品120' });
  await page.waitForTimeout(900);
  await page.locator('.chart-card').screenshot({ path: testInfo.outputPath('long-ranking-last-page.png') });
  expect(requests.filter(item => item.method !== 'GET')).toEqual([]);
});
test('narrow heatmap keeps a real plot area and scrolls within its card', async ({ page }, testInfo) => {
  const { state, requests } = await enterpriseApi(page);
  const groups = ['early_key_feature / D30', 'early_key_feature / D60', 'early_key_feature / D90', 'no_early_key_feature / D30', 'no_early_key_feature / D60', 'no_early_key_feature / D90'];
  const rows = Array.from({ length: 22 }, (_, index) => groups.map((group, i) => ['队列' + (index + 1), group, 70 + (index + i) % 31])).flat();
  state.run.artifacts.queries = [{ id: 'retention', columns: ['cohort', 'group', 'rate'], rows }];
  state.run.artifacts.charts = [{ type: 'heatmap', title: '账户留存', query_id: 'retention', x: 'cohort', group_by: 'group', y: ['rate'], unit: '%' }];
  await page.setViewportSize({ width: 390, height: 844 }); await page.goto('/');
  await page.addInitScript(() => localStorage.setItem('insight-agents.workspace.v1', JSON.stringify({ scenarioId: 'supply-chain', mode: 'live', runId: 'run-existing' })));
  await page.reload(); await expect(page.locator('.chart-canvas canvas')).toHaveCount(1);
  await expect(page.locator('.chart-scroll-hint')).toBeVisible();
  const geometry = await page.evaluate(async () => {
    const url = performance.getEntriesByType('resource').map(item => item.name).find(name => name.includes('/echarts_core.js'));
    const { getInstanceByDom } = await import(url);
    const canvas = document.querySelector('.chart-canvas'), viewport = document.querySelector('.chart-viewport');
    const instance = getInstanceByDom(canvas), left = instance.convertToPixel({ seriesIndex: 0 }, [0, 0]), right = instance.convertToPixel({ seriesIndex: 0 }, [21, 0]);
    viewport.scrollLeft = 350;
    return { plotWidth: right[0] - left[0], points: instance.getOption().series[0].data.length, canvas: canvas.clientWidth, viewport: viewport.clientWidth, scroll: viewport.scrollLeft };
  });
  expect(geometry.points).toBe(132); expect(geometry.plotWidth).toBeGreaterThan(500);
  expect(geometry.canvas).toBeGreaterThan(geometry.viewport); expect(geometry.scroll).toBe(350);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.locator('.chart-card').screenshot({ path: testInfo.outputPath('narrow-heatmap-scrolled.png') });
  expect(requests.filter(item => item.method !== 'GET')).toEqual([]);
});
test('wide saved table scrolls internally with visible sticky headers and no page overflow', async ({ page }, testInfo) => {
  const { state } = await enterpriseApi(page);
  const columns = Array.from({ length: 14 }, (_, index) => 'business_dimension_' + index);
  state.run.artifacts.queries = [{ id: 'wide', columns, rows: Array.from({ length: 40 }, (_, index) => columns.map((_, column) => column ? 12345.6789 + index : '客户对象 ' + index)) }];
  state.run.artifacts.charts = [];
  await page.setViewportSize({ width: 1280, height: 1000 }); await page.goto('/'); await page.locator('.recent-run').first().click();
  await page.getByLabel('每页行数 wide').selectOption('50');
  await expect(page.locator('.saved-query tbody tr')).toHaveCount(40);
  const dimensions = await page.locator('.saved-query .table-scroll').evaluate(node => { node.scrollLeft = 480; node.scrollTop = 170; return { width: node.clientWidth, contentWidth: node.scrollWidth, scrollTop: node.scrollTop }; });
  expect(dimensions.contentWidth).toBeGreaterThan(dimensions.width); expect(dimensions.scrollTop).toBe(170);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(1280);
  await page.locator('.saved-query').scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath('saved-wide-table.png'), fullPage: true });
});
