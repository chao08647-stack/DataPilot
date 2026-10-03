import { test, expect } from '@playwright/test';

const requestedRuns = JSON.parse(process.env.INSIGHT_BROWSER_RUNS || '{}');
test.describe('opt-in saved real analysis integration (strictly GET-only)', () => {
  test.skip(process.env.INSIGHT_BROWSER_LIVE !== '1', 'Explicit INSIGHT_BROWSER_LIVE=1 required; ordinary tests are fully mocked.');
  for (const domainId of ['ecommerce', 'retail', 'saas']) {
    test(domainId + ': persisted real history, chart data and SQL evidence', async ({ page, request }, testInfo) => {
      const mutations = [], errors = [];
      // Guard, not just observe: browser smoke checks can never submit real work.
      await page.route('**/api/v1/**', route => {
        if (route.request().method() !== 'GET') { mutations.push(route.request().url()); return route.abort('blockedbyclient'); }
        return route.continue();
      });
      page.on('request', req => { if (req.method() !== 'GET' && req.url().includes('/api/')) mutations.push(req.url()); });
      page.on('pageerror', error => errors.push(error.message));
      const catalogResponse = await request.get('/api/v1/questions');
      expect(catalogResponse.ok()).toBeTruthy();
      const catalog = await catalogResponse.json();
      expect(catalog.some(item => item.domain_id === domainId)).toBeTruthy();
      const domainResponse = await request.get('/api/v1/domains/' + domainId);
      expect(domainResponse.ok()).toBeTruthy(); const domain = await domainResponse.json();
      const response = await request.get('/api/v1/runs?domain_id=' + domainId + '&status=completed&limit=100&offset=0');
      expect(response.ok()).toBeTruthy(); const history = await response.json();
      expect(Array.isArray(history.items)).toBeTruthy();
      const target = requestedRuns[domainId] ? history.items.find(run => run.run_id === requestedRuns[domainId]) : history.items.find(run => !run.partial && run.origin === 'question_catalog' && String(run.model_version) === String(domain.model_version));
      test.skip(!target, 'This business domain has no completed current-version real catalog task yet; not manufacturing one.');
      const detailResponse = await request.get('/api/v1/runs/' + target.run_id);
      expect(detailResponse.ok()).toBeTruthy(); const run = await detailResponse.json();
      for (const query of run.artifacts.queries || []) {
        expect(query.rows.length).toBeLessThanOrEqual(50);
        if (query.result_ref) {
          const rowsResponse = await request.get('/api/v1/runs/' + run.run_id + '/queries/' + query.id + '/rows?offset=0&limit=10');
          expect(rowsResponse.ok()).toBeTruthy(); const rows = await rowsResponse.json();
          expect(rows.columns).toEqual(query.columns); expect(rows.total).toBe(query.total_rows);
          expect(rows.rows.length).toBeLessThanOrEqual(10);
        }
      }
      await page.addInitScript(({ id, runId }) => {
        localStorage.setItem('insight-agents.enterprise.domain', id);
        localStorage.setItem('insight-agents.workspace.v1', JSON.stringify({ scenarioId: id, mode: 'live', runId }));
      }, { id: domainId, runId: run.run_id });
      await page.goto('/#analysis');
      await expect(page.locator('.question-card h1')).toHaveText(run.question);
      await expect(page.locator('.run-status')).toHaveText(run.artifacts.partial ? '部分结果 · 待复核' : '已完成');
      await expect(page.locator('.answer-summary')).toContainText(run.artifacts.analysis.summary.slice(0, 20));
      await expect(page.locator('.chart-card')).toHaveCount(run.artifacts.charts.length);
      await expect(page.locator('.chart-loading')).toHaveCount(0);
      await expect(page.getByText('加载图表组件中…', { exact: true })).toHaveCount(0);
      if (run.artifacts.charts.some(chart => !['table', 'kpi'].includes(chart.type))) await expect(page.locator('.chart-canvas canvas').first()).toBeAttached();
      await page.waitForTimeout(1000);
      await page.screenshot({ path: testInfo.outputPath(domainId + '-real-history.png'), fullPage: true });
      if (await page.locator('.chart-card').count()) {
        await page.locator('.chart-card').first().scrollIntoViewIfNeeded();
        await page.screenshot({ path: testInfo.outputPath(domainId + '-real-charts.png'), fullPage: true });
      }
      await page.locator('.evidence-panel>summary').click();
      await expect(page.locator('.sql-code').first()).not.toBeEmpty();
      const pageable = (run.artifacts.queries || []).find(query => (query.total_rows || query.rows.length) > 10 && query.result_ref);
      if (pageable) {
        const table = page.locator('.saved-query').filter({ has: page.getByLabel('下一页 ' + pageable.id, { exact: true }) });
        if (await table.getAttribute('open') === null) await table.locator('summary').click();
        await expect(table.locator('tbody tr')).toHaveCount(10);
        const firstPage = await table.locator('tbody tr').first().innerText();
        await table.getByLabel('下一页 ' + pageable.id, { exact: true }).click();
        await expect(table.locator('tbody tr').first()).not.toHaveText(firstPage);
        await expect(table.locator('.table-pagination')).toContainText('共 ' + Number(pageable.total_rows).toLocaleString('zh-CN') + ' 行');
        if (pageable.columns.length >= 7) {
          const sizes = await table.locator('.table-scroll').evaluate(node => { node.scrollLeft = node.scrollWidth; return { width: node.clientWidth, content: node.scrollWidth, left: node.scrollLeft }; });
          if (sizes.content > sizes.width) expect(sizes.left).toBeGreaterThan(0);
        }
        await table.getByLabel('上一页 ' + pageable.id, { exact: true }).click();
      }
      const focus = run.artifacts.charts.findIndex(chart => ['funnel', 'waterfall', 'heatmap'].includes(chart.type));
      const focusIndex = focus >= 0 ? focus : run.artifacts.charts.findIndex(chart => !['kpi', 'table'].includes(chart.type));
      if (focusIndex >= 0) for (const width of [1440, 1280, 390]) {
        await page.setViewportSize({ width, height: width < 600 ? 844 : 1100 });
        const card = page.locator('.chart-card').nth(focusIndex);
        await card.evaluate(node => { const scroll = document.querySelector('.analysis-scroll'); scroll.scrollTop += node.getBoundingClientRect().top - scroll.getBoundingClientRect().top - 16; });
        await expect(card.locator('.chart-canvas canvas')).toBeAttached();
        await page.waitForTimeout(450);
        expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
        await card.screenshot({ path: testInfo.outputPath(domainId + '-' + run.artifacts.charts[focusIndex].type + '-' + width + '.png') });
        if (width < 600) {
          const moved = await card.locator('.chart-viewport').evaluate(node => { node.scrollLeft = node.scrollWidth; return node.scrollLeft; });
          if (moved > 0) await card.screenshot({ path: testInfo.outputPath(domainId + '-' + run.artifacts.charts[focusIndex].type + '-' + width + '-scrolled.png') });
        }
      }
      expect(mutations).toEqual([]); expect(errors).toEqual([]);
      if (domainId === 'ecommerce') {
        await page.setViewportSize({ width: 1440, height: 1000 });
        await page.getByRole('button', { name: '新建分析', exact: true }).click();
        await expect(page.getByRole('heading', { name: '今天，想分析什么？' })).toBeVisible();
        await expect(page.locator('.question-catalog button')).not.toHaveCount(0);
        await page.screenshot({ path: testInfo.outputPath('chat-home-real-catalog.png') });
        expect(mutations).toEqual([]);
      }
    });
  }
});
