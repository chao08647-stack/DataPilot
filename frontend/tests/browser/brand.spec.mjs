import { test, expect } from '@playwright/test';
import { enterpriseApi } from './fixtures.mjs';

test('DataPilot branding loads across home and saved replies without new analysis', async ({ page }, testInfo) => {
  const { requests } = await enterpriseApi(page);
  await page.goto('/');
  await expect(page).toHaveTitle('DataPilot · 数据分析工作区');
  const brand = page.getByRole('link', { name: 'DataPilot 分析工作台' });
  await expect(brand).toHaveText('DataPilot');
  await expect(brand.locator('strong')).toHaveCSS('color', 'rgb(27, 32, 94)');
  await expect(brand.locator('strong span')).toHaveCSS('color', 'rgb(0, 155, 159)');
  await expect(page.locator('link[rel="icon"]')).toHaveAttribute('href', '/brand/datapilot-icon.png');
  await expect(page.getByRole('img', { name: 'DataPilot' })).toBeVisible();
  await expect.poll(() => page.locator('.brand-icon').evaluateAll(images => images.every(image => image.complete && image.naturalWidth > 0))).toBe(true);
  // Real alpha rather than a baked white/checkerboard background.
  const alpha = await page.locator('.welcome-icon img').evaluate(image => {
    const canvas = document.createElement('canvas'); canvas.width = image.naturalWidth; canvas.height = image.naturalHeight;
    const context = canvas.getContext('2d'); context.drawImage(image, 0, 0);
    return context.getImageData(0, 0, 1, 1).data[3];
  });
  expect(alpha).toBe(0);
  await page.screenshot({ path: testInfo.outputPath('datapilot-home.png') });
  await page.locator('.recent-run').first().click();
  await expect(page.locator('.assistant-identity > strong')).toHaveText('DataPilot');
  await expect(page.locator('.assistant-identity img')).toHaveAttribute('src', '/brand/datapilot-icon.png');
  await expect(page.locator('.answer-summary')).toBeVisible();
  await expect(page.locator('.chart-canvas canvas')).toHaveCount(3);
  await expect(page.locator('body')).not.toContainText('Insight Agents');
  await page.screenshot({ path: testInfo.outputPath('datapilot-saved-answer.png') });
  const session = await page.evaluate(() => JSON.parse(localStorage.getItem('insight-agents.workspace.v1')));
  expect(session.runId).toBe('run-existing');
  expect(requests.filter(item => item.method !== 'GET')).toEqual([]);
});

for (const width of [1280, 390]) {
  test('DataPilot icon remains legible without layout overflow at ' + width, async ({ page }, testInfo) => {
    const { requests } = await enterpriseApi(page);
    await page.setViewportSize({ width, height: width < 600 ? 844 : 1000 });
    await page.goto('/');
    const icon = page.getByRole('img', { name: 'DataPilot' });
    await expect(icon).toBeVisible();
    await icon.evaluate(image => image.decode());
    const bounds = await icon.boundingBox();
    expect(bounds.width).toBe(48);
    expect(bounds.height).toBe(48);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
    await page.screenshot({ path: testInfo.outputPath(`datapilot-home-${width}.png`) });
    expect(requests.filter(item => item.method !== 'GET')).toEqual([]);
  });
}
