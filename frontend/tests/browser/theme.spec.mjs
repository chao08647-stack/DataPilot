import { test, expect } from '@playwright/test';
import { enterpriseApi } from './fixtures.mjs';

const blue = 'rgb(22, 100, 255)', hover = 'rgb(14, 75, 210)';
const ink = 'rgb(31, 35, 41)', muted = 'rgb(102, 112, 133)';
const gray = 'rgb(235, 238, 242)', white = 'rgb(255, 255, 255)', outline = 'rgb(205, 210, 218)';

test('long saved conclusions use the full result width with outlined actions and a white composer', async ({ page }, testInfo) => {
  const { state, requests } = await enterpriseApi(page);
  const original = state.run.artifacts.analysis.summary.repeat(14);
  state.run.artifacts.analysis.summary = original;
  await page.setViewportSize({ width: 1920, height: 1080 });
  await page.goto('/');
  await page.locator('.recent-run').first().click();
  await expect(page.locator('.answer-summary')).toHaveText(original);
  await page.locator('.chat-brand .brand-icon').evaluate(image => image.decode());
  await expect(page.locator('.chart-canvas canvas')).toHaveCount(3);
  const results = await page.locator('.result-section').boundingBox();
  const answer = await page.locator('.answer-card').boundingBox();
  expect(answer.width).toBeCloseTo(results.width, 0);
  const textWidth = await page.locator('.answer-summary').evaluate(node => {
    const range = document.createRange(); range.selectNodeContents(node);
    return range.getBoundingClientRect().width;
  });
  expect(textWidth).toBeGreaterThan(1200);
  await expect(page.locator('.composer')).toHaveCSS('background-color', white);
  await expect(page.locator('.composer')).toHaveCSS('border-color', 'rgb(175, 196, 228)');
  await page.screenshot({ path: testInfo.outputPath('full-width-answer-outlined-actions.png') });
  expect(state.run.artifacts.analysis.summary).toBe(original);
  expect(requests.filter(item => item.method !== 'GET')).toEqual([]);
});

test('professional blue hierarchy, focus, hover and disabled states are distinct', async ({ page }, testInfo) => {
  const { requests, state } = await enterpriseApi(page);
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.goto('/');
  await expect(page.locator('.enterprise-sidebar')).toHaveCSS('background-color', 'rgb(245, 247, 250)');
  await expect(page.locator('.analysis-welcome h1')).toHaveCSS('color', ink);
  await expect(page.locator('.chat-logo').first()).toHaveCSS('color', blue);
  await expect(page.locator('.composer-caption')).toHaveCSS('color', muted);
  await expect(page.locator('.composer')).toHaveCSS('border-color', 'rgb(175, 196, 228)');
  await expect(page.locator('.composer')).toHaveCSS('background-color', white);
  for (const button of await page.locator('.advanced-nav button').all()) {
    await expect(button).toHaveCSS('border-width', '1px');
    await expect(button).toHaveCSS('border-color', outline);
    await expect(button).toHaveCSS('background-color', white);
  }
  const start = page.getByRole('button', { name: '新建分析', exact: true });
  await expect(start).toHaveCSS('background-color', white);
  await expect(start).toHaveCSS('border-width', '1px');
  await expect(start).toHaveCSS('border-color', outline);
  await expect(start).toHaveCSS('color', ink);
  await expect(start).toHaveCSS('background-image', 'none');
  await start.hover();
  await expect(start).not.toHaveCSS('background-color', blue);
  await expect(start).not.toHaveCSS('background-color', hover);
  await start.focus();
  await expect(start).toHaveCSS('outline-color', blue);

  const send = page.getByRole('button', { name: '发送分析问题' });
  await expect(send).toBeDisabled();
  await expect(send).toHaveCSS('background-color', gray);
  await send.hover();
  await expect(send).toHaveCSS('background-color', gray);
  await page.getByLabel('分析问题', { exact: true }).fill('比较各渠道收入');
  await expect(page.locator('.composer')).toHaveCSS('border-color', blue);
  await expect(send).toBeEnabled();
  await page.locator('.analysis-welcome h1').hover();
  await expect(send).toHaveCSS('background-color', blue);
  await send.hover();
  await expect(send).toHaveCSS('background-color', hover);
  await page.getByLabel('分析问题', { exact: true }).fill('');
  await page.locator('.analysis-welcome h1').click();
  await page.screenshot({ path: testInfo.outputPath('blue-home.png') });

  const history = page.locator('.recent-run').first();
  await history.hover();
  await expect(history).toHaveCSS('background-color', gray);
  await history.click();
  await expect(history).toHaveCSS('background-color', 'rgb(234, 242, 255)');
  await expect(history.locator('strong')).toHaveCSS('color', ink);
  await expect(history.locator('time')).toHaveCSS('color', 'rgb(78, 89, 105)');
  await expect(page.locator('.answer-summary')).toHaveCSS('color', ink);

  state.run.status = 'waiting_for_input';
  state.run.artifacts = { clarification: { question: '按哪个统计口径？' } };
  await page.reload();
  const resume = page.getByRole('button', { name: '补充并继续' });
  await expect(resume).toBeDisabled();
  await expect(resume).toHaveCSS('background-color', gray);
  await resume.hover();
  await expect(resume).toHaveCSS('background-color', gray);
  await page.getByLabel('补充说明').fill('按净收入统计');
  await page.locator('.clarification-card strong').hover();
  await expect(resume).toHaveCSS('background-color', blue);
  await page.screenshot({ path: testInfo.outputPath('blue-clarification.png') });
  expect(requests.filter(item => item.method !== 'GET')).toEqual([]);
});

test('advanced panels and source form share the neutral theme without mutations', async ({ page }, testInfo) => {
  const { requests } = await enterpriseApi(page);
  await page.goto('/#data');
  await expect(page.locator('.source-card').first().locator('h3')).toHaveCSS('color', ink);
  await expect(page.locator('.enterprise-tabs button.active')).toHaveCSS('color', blue);
  await page.screenshot({ path: testInfo.outputPath('blue-data.png') });
  await page.getByRole('button', { name: '接入数据源', exact: true }).click();
  await expect(page.locator('.enterprise-modal')).toHaveCSS('background-color', 'rgb(255, 255, 255)');
  await expect(page.locator('.enterprise-modal h2')).toHaveCSS('color', ink);
  await expect(page.getByLabel('名称', { exact: true })).toHaveCSS('color', ink);
  await page.screenshot({ path: testInfo.outputPath('blue-source-form.png') });
  await page.getByRole('button', { name: '关闭数据源配置' }).click();
  await page.getByRole('button', { name: '分析方法', exact: true }).click();
  await expect(page.locator('.skill-card h3')).toHaveCSS('color', ink);
  await expect(page.locator('.skill-card h3>span')).toHaveCSS('font-size', '12px');
  await page.screenshot({ path: testInfo.outputPath('blue-methods.png') });
  await page.getByRole('button', { name: '关闭资源面板' }).click();
  await page.getByRole('button', { name: '知识与偏好', exact: true }).click();
  await expect(page.locator('.memory-card h3')).toHaveCSS('color', ink);
  for (const count of await page.locator('.memory-filters button>span').all()) await expect(count).toHaveCSS('font-size', '12px');
  await page.screenshot({ path: testInfo.outputPath('blue-memory.png') });
  expect(requests.filter(item => item.method !== 'GET')).toEqual([]);
});

test('loading and error presentation retain distinct status meaning without new work', async ({ page }, testInfo) => {
  const { state, requests } = await enterpriseApi(page);
  state.run.status = 'running'; state.run.artifacts = {};
  await page.route('**/api/v1/runs/*/events?*', route => route.fulfill({ status: 200, contentType: 'text/event-stream', body: 'event: stream.closed\ndata: {}\n\n' }));
  await page.goto('/');
  await page.locator('.recent-run').first().click();
  await expect(page.locator('.thinking-card')).toBeVisible();
  await expect(page.locator('.thinking-card .spinner')).toHaveCSS('border-top-color', blue);
  await expect(page.locator('.thinking-card strong')).toHaveCSS('color', ink);
  await page.screenshot({ path: testInfo.outputPath('blue-running.png') });
  state.run.status = 'failed'; state.run.error = '查询未通过校验，请调整条件。';
  await page.reload();
  await expect(page.getByRole('alert')).toContainText('查询未通过校验');
  await expect(page.getByRole('alert')).toHaveCSS('color', 'rgb(165, 92, 92)');
  await expect(page.locator('.thinking-card')).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath('blue-error.png') });
  expect(requests.filter(item => item.method !== 'GET')).toEqual([]);
});

test('neutral text meets 4.5 to 1 contrast on main and sidebar surfaces', async ({ page }) => {
  await enterpriseApi(page);
  await page.goto('/');
  const ratios = await page.evaluate(() => {
    const vars = getComputedStyle(document.documentElement);
    const luminance = name => {
      const hex = vars.getPropertyValue(name).trim().slice(1);
      const rgb = hex.match(/../g).map(part => parseInt(part, 16) / 255).map(c => c <= .04045 ? c / 12.92 : ((c + .055) / 1.055) ** 2.4);
      return rgb[0] * .2126 + rgb[1] * .7152 + rgb[2] * .0722;
    };
    // Selected blue rows use secondary text, not muted captions.
    return ['--ink', '--text-secondary', '--muted'].flatMap(fg => ['--surface', '--surface-subtle', ...(fg === '--muted' ? [] : ['--selected'])].map(bg => (luminance(bg) + .05) / (luminance(fg) + .05)))
      .concat((luminance('--surface') + .05) / (luminance('--blue') + .05));
  });
  for (const ratio of ratios) expect(ratio).toBeGreaterThanOrEqual(4.5);
});
