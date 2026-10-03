import { test, expect } from '@playwright/test';
import { enterpriseApi } from './fixtures.mjs';

test('neutral product labels preserve source metadata and do not create work', async ({ page }) => {
  const { state, requests } = await enterpriseApi(page);
  state.sources[0].name = '供应链与履约 · 合成数据';
  state.domains[0].description = '仅合成数据，结论必须从实际查询计算。';
  await page.goto('/');
  await expect(page.locator('.composer-caption')).toContainText('只读分析');
  await expect(page.locator('.composer-caption')).not.toContainText('合成数据');
  await page.getByRole('button', { name: '数据与语义', exact: true }).click();
  await expect(page.locator('.source-card').first().locator('h3')).toHaveText('供应链与履约');
  await expect(page.locator('.source-card').first()).toContainText('本地数据源');
  await expect(page.locator('.source-grid')).not.toContainText('内置示例');
  await expect(page.locator('.source-grid')).not.toContainText('合成数据');
  expect(state.sources[0].builtin).toBe(true);
  expect(state.sources[0].name).toBe('供应链与履约 · 合成数据');
  expect(requests.filter(item => item.method !== 'GET')).toEqual([]);
});

test('a deleted saved run clears the stale local restore reference without resubmitting', async ({ page }) => {
  const { requests } = await enterpriseApi(page);
  await page.route('**/api/v1/runs/removed-run', route => route.fulfill({ status: 404, contentType: 'application/json', body: JSON.stringify({ detail: 'Not found' }) }));
  await page.addInitScript(() => {
    // Seed only once: a reload must not manufacture the removed reference again.
    if (!sessionStorage.getItem('cleanup-test-seeded')) {
      localStorage.setItem('insight-agents.workspace.v1', JSON.stringify({ scenarioId: 'supply-chain', mode: 'live', runId: 'removed-run' }));
      sessionStorage.setItem('cleanup-test-seeded', '1');
    }
  });
  await page.goto('/');
  await expect(page.getByRole('alert')).toContainText('已删除或不存在');
  await expect(page.locator('.question-card')).toHaveCount(0);
  expect(await page.evaluate(() => localStorage.getItem('insight-agents.workspace.v1'))).toBeNull();
  await page.reload();
  await expect(page.getByRole('heading', { name: '今天，想分析什么？' })).toBeVisible();
  await expect(page.getByRole('alert')).toHaveCount(0);
  expect(requests.filter(item => item.method !== 'GET')).toEqual([]);
});

test('retired dashboards return to chat without reading or mutating stored boards', async ({ page }) => {
  const { state, requests } = await enterpriseApi(page);
  const originalBoards = structuredClone(state.boards);
  await page.goto('/#dashboards');
  await expect(page.getByRole('heading', { name: '今天，想分析什么？' })).toBeVisible();
  await expect(page.getByRole('button', { name: /看板/ })).toHaveCount(0);
  await expect(page.locator('.enterprise-sidebar nav')).toHaveCount(0);
  await page.locator('.recent-run').first().click();
  await expect(page.locator('.chart-canvas canvas')).toHaveCount(3);
  await expect(page.getByRole('button', { name: /看板/ })).toHaveCount(0);
  await page.locator('.saved-query').first().locator('summary').click();
  await expect(page.locator('.saved-query').first().locator('tbody tr')).toHaveCount(4);
  expect(requests.filter(item => item.path.startsWith('/dashboards'))).toEqual([]);
  expect(requests.filter(item => item.method !== 'GET')).toEqual([]);
  expect(state.boards).toEqual(originalBoards);
});

test('advanced tools preserve draft and history; new analysis clears thread only on user action', async ({ page }) => {
  const { requests } = await enterpriseApi(page);
  await page.goto('/');
  await page.getByLabel('分析问题', { exact: true }).fill('先看看各渠道的变化');
  await page.getByRole('button', { name: '数据与语义', exact: true }).click();
  await page.getByRole('button', { name: '返回分析', exact: true }).click();
  await expect(page.getByLabel('分析问题', { exact: true })).toHaveValue('先看看各渠道的变化');
  await page.getByRole('button', { name: '分析方法', exact: true }).click();
  await expect(page.locator('.skill-card')).toContainText('跨表粒度检查');
  await page.getByRole('button', { name: '关闭资源面板' }).click();
  await expect(page.getByLabel('分析问题', { exact: true })).toHaveValue('先看看各渠道的变化');
  await page.locator('.recent-run').first().click();
  await expect(page.locator('.answer-summary')).toBeVisible();
  await expect(page.locator('.evidence-panel')).not.toHaveAttribute('open');
  await expect(page.getByRole('button', { name: '查看分析过程', exact: true })).toBeVisible();
  await expect(page.getByRole('dialog', { name: '分析过程', exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: '新建分析', exact: true }).click();
  await expect(page.getByLabel('分析问题', { exact: true })).toHaveValue('');
  await expect(page.locator('.question-card')).toHaveCount(0);
  expect(await page.evaluate(() => localStorage.getItem('insight-agents.workspace.v1'))).toBeNull();
  expect(requests.filter(item => item.method !== 'GET')).toEqual([]);
});

for (const width of [2124, 1920, 1440, 1280, 390]) {
  test('chat home, saved answer and clarification layout at ' + width, async ({ page }, testInfo) => {
    const { state, requests } = await enterpriseApi(page);
    const errors = []; page.on('pageerror', error => errors.push(error.message));
    await page.setViewportSize({ width, height: width < 600 ? 844 : 1000 });
    await page.goto('/');
    await expect(page.getByRole('heading', { name: '今天，想分析什么？' })).toBeVisible();
    await page.locator('.welcome-icon img').evaluate(image => image.decode());
    const home = await page.locator('.composer').boundingBox();
    expect(home.height).toBeCloseTo(152, 0);
    expect(home.width).toBeLessThanOrEqual(800);
    const recommendation = await page.locator('.question-catalog button').boundingBox();
    const catalog = await page.locator('.question-catalog').boundingBox();
    expect(recommendation.height).toBeCloseTo(60, 0);
    expect(recommendation.width).toBeCloseTo(catalog.width, 0);
    await page.screenshot({ path: testInfo.outputPath('chat-home-' + width + '.png') });
    await page.locator('.question-catalog button').first().click();
    await expect(page.getByLabel('分析问题', { exact: true })).toHaveValue('比较各地区履约成本');
    if (width < 600) {
      await expect(page.locator('.enterprise-sidebar')).not.toBeVisible();
      await page.getByRole('button', { name: '展开历史对话' }).click();
      await expect(page.locator('.enterprise-sidebar')).toBeVisible();
    }
    await page.locator('.recent-run').first().click();
    await expect(page.locator('.answer-summary')).toBeVisible();
    await expect(page.locator('.chart-canvas canvas')).toHaveCount(3);
    await page.waitForTimeout(700);
    await page.screenshot({ path: testInfo.outputPath('chat-answer-' + width + '.png') });
    if (width < 600) await expect(page.locator('.enterprise-sidebar')).not.toBeVisible();
    const composer = await page.locator('.composer').boundingBox();
    expect(composer.height).toBeCloseTo(104, 0);
    expect(composer.y + composer.height).toBeLessThan(width < 600 ? 844 : 1000);
    const results = await page.locator('.result-section').boundingBox();
    expect(results.width).toBeLessThanOrEqual(1600);
    expect(results.width).toBeCloseTo(composer.width, 0);
    expect(results.x).toBeCloseTo(composer.x, 0);
    if (width >= 1280) {
      expect((await page.locator('.enterprise-sidebar').boundingBox()).width).toBeCloseTo(240, 0);
      const usableMainWidth = await page.locator('.analysis-scroll').evaluate(node => node.clientWidth);
      expect(results.width).toBeCloseTo(Math.min(1600, usableMainWidth - 64), 0);
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
    state.run.status = 'waiting_for_input';
    state.run.artifacts = { clarification: { question: '本次收入按成交金额，还是扣除退款后的净收入统计？' } };
    await page.reload();
    await expect(page.locator('.clarification-card')).toBeVisible();
    await expect(page.getByLabel('补充说明')).toBeVisible();
    await expect(page.getByLabel('分析问题', { exact: true })).toBeDisabled();
    await page.screenshot({ path: testInfo.outputPath('chat-clarify-' + width + '.png') });
    expect(requests.filter(item => item.method !== 'GET')).toEqual([]);
    expect(errors).toEqual([]);
  });
}
