import { test, expect } from '@playwright/test';
import { enterpriseApi, completed } from './fixtures.mjs';

const questionInput = page => page.getByLabel('分析问题', { exact: true });
const processButton = page => page.getByRole('button', { name: '查看分析过程', exact: true });
const processDialog = page => page.getByRole('dialog', { name: '分析过程', exact: true });
const nonReads = requests => requests.filter(item => item.method !== 'GET');

test('an empty history and empty question catalog stay genuinely empty', async ({ page }) => {
  const { requests } = await enterpriseApi(page);
  await page.route('**/api/v1/runs?*', route => route.fulfill({ contentType: 'application/json', body: JSON.stringify({ items: [], total: 0, offset: 0, limit: 20 }) }));
  await page.route('**/api/v1/questions', route => route.fulfill({ contentType: 'application/json', body: '[]' }));
  await page.goto('/');
  await expect(page.getByRole('heading', { name: '今天，想分析什么？', exact: true })).toBeVisible();
  await expect(page.locator('.recent-run')).toHaveCount(0);
  await expect(page.locator('.question-catalog')).toHaveCount(0);
  await expect(page.locator('.result-section')).toHaveCount(0);
  await expect(page.getByRole('button', { name: '发送分析问题' })).toBeDisabled();
  expect(nonReads(requests)).toEqual([]);
});

test('multiple existing suggestions form two columns and collapse to one on mobile', async ({ page }) => {
  const { requests } = await enterpriseApi(page);
  const questions = [
    { id: 'costs', title: '地区履约成本对比', question: '比较各地区履约成本', domain_id: 'supply-chain' },
    { id: 'existing-second', title: '另一条测试问题', question: '核对本月各地区运输金额', domain_id: 'supply-chain' },
  ];
  await page.route('**/api/v1/questions', route => route.fulfill({ contentType: 'application/json', body: JSON.stringify(questions) }));
  await page.goto('/');
  const cards = page.locator('.question-catalog button');
  await expect(cards).toHaveCount(2);
  let first = await cards.nth(0).boundingBox(), second = await cards.nth(1).boundingBox();
  expect(first.y).toBeCloseTo(second.y, 0);
  expect(second.x).toBeGreaterThan(first.x);
  await page.setViewportSize({ width: 390, height: 844 });
  first = await cards.nth(0).boundingBox(); second = await cards.nth(1).boundingBox();
  expect(first.x).toBeCloseTo(second.x, 0);
  expect(second.y).toBeGreaterThan(first.y);
  await cards.nth(1).click();
  await expect(questionInput(page)).toHaveValue(questions[1].question);
  expect(nonReads(requests)).toEqual([]);
});

test('catalog titles require an exact question match and long originals stay expandable and searchable', async ({ page }, testInfo) => {
  const { state, requests } = await enterpriseApi(page);
  const question = '请比较各地区履约成本和退款变化，并核对统计时间及各渠道的归属口径。'.repeat(14);
  state.additionalRuns = [{ ...structuredClone(completed), run_id: 'long-question', question }];
  await page.goto('/');
  await page.locator('.recent-run').first().click();
  await expect(page.locator('.analysis-toolbar strong')).toHaveText('地区履约成本对比');
  await expect(page.locator('.analysis-toolbar strong')).toHaveAttribute('title', completed.question);
  await page.locator('.recent-run').nth(1).click();
  await expect(page.locator('.analysis-toolbar strong')).toHaveText(Array.from(question).slice(0, 24).join('') + '…');
  await expect(page.locator('.analysis-toolbar strong')).toHaveAttribute('title', question);
  const original = page.locator('.question-card h1');
  await expect(original).toHaveText(question);
  await expect(original).toHaveCSS('-webkit-line-clamp', '4');
  const collapsed = await original.boundingBox();
  expect(collapsed.height).toBe(26 * 4);
  await page.locator('.chat-brand .brand-icon').evaluate(image => image.decode());
  await expect(page.locator('.chart-canvas canvas')).toHaveCount(3);
  await page.screenshot({ path: testInfo.outputPath('long-question-collapsed.png') });
  await page.getByRole('button', { name: '展开全文', exact: true }).click();
  expect((await original.boundingBox()).height).toBeGreaterThan(collapsed.height);
  await page.getByRole('button', { name: '收起', exact: true }).click();
  expect((await original.boundingBox()).height).toBeCloseTo(collapsed.height, 0);
  await page.getByLabel('搜索分析记录').fill('统计时间及各渠道');
  await expect(page.locator('.recent-run')).toHaveCount(1);
  expect(state.additionalRuns[0].question).toBe(question);
  expect(nonReads(requests)).toEqual([]);
});

test('composer grows to a bounded height and shrinks after editing, suggestions and page changes', async ({ page }) => {
  const { requests } = await enterpriseApi(page);
  await page.goto('/');
  const height = () => page.locator('.composer').evaluate(node => node.getBoundingClientRect().height);
  await expect.poll(height).toBe(152);
  const longInput = Array.from({ length: 40 }, (_, i) => `分析条件 ${i + 1}：保留原有业务口径与数据范围。`).join('\n');
  await questionInput(page).fill(longInput);
  await expect.poll(height).toBe(240);
  const textarea = await questionInput(page).evaluate(node => ({ scroll: node.scrollHeight, height: node.clientHeight, overflow: getComputedStyle(node).overflowY }));
  expect(textarea.scroll).toBeGreaterThan(textarea.height);
  expect(textarea.overflow).toBe('auto');
  await page.getByRole('button', { name: '数据与语义', exact: true }).click();
  await page.getByRole('button', { name: '返回分析', exact: true }).click();
  await expect(questionInput(page)).toHaveValue(longInput);
  await expect.poll(height).toBe(240);
  await page.locator('.question-catalog button').first().click();
  await expect(questionInput(page)).toHaveValue(completed.question);
  await expect.poll(height).toBe(152);
  await questionInput(page).fill(longInput);
  await page.locator('.recent-run').first().click();
  await expect(questionInput(page)).toHaveValue('');
  await expect.poll(height).toBe(104);
  await questionInput(page).fill(longInput);
  await expect.poll(height).toBe(240);
  await questionInput(page).fill('');
  await expect.poll(height).toBe(104);
  await page.getByRole('button', { name: '新建分析', exact: true }).click();
  await expect.poll(height).toBe(152);
  await expect(page.locator('.composer-bottom')).not.toContainText('新会话');
  await expect(page.locator('.composer-bottom')).not.toContainText('继续本次会话');
  const send = page.getByRole('button', { name: '发送分析问题', exact: true });
  expect((await send.boundingBox()).width).toBe(40);
  expect((await send.boundingBox()).height).toBe(40);
  await expect(send).toHaveAttribute('title', /发送|分析/);
  expect(nonReads(requests)).toEqual([]);
});

test('IME Enter and Shift Enter do not submit; plain Enter clears the draft and preserves HITL', async ({ page }) => {
  const { requests } = await enterpriseApi(page);
  await page.goto('/');
  const input = questionInput(page);
  await input.fill('检查中文输入法提交保护');
  await input.dispatchEvent('keydown', { key: 'Enter', code: 'Enter', isComposing: true });
  await expect(input).toHaveValue('检查中文输入法提交保护');
  expect(nonReads(requests)).toEqual([]);
  await input.press('End');
  await input.press('Shift+Enter');
  await expect(input).toHaveValue('检查中文输入法提交保护\n');
  expect(nonReads(requests)).toEqual([]);
  await input.press('Enter');
  await expect(page.locator('.clarification-card')).toBeVisible();
  await expect(input).toHaveValue('');
  await expect(input).toBeDisabled();
  expect((await page.locator('.composer').boundingBox()).height).toBe(104);
  expect(requests.filter(item => item.path === '/runs' && item.method === 'POST')).toHaveLength(1);
});

test('trace drawer keeps full escaped payloads, traps focus and restores it without data mutations', async ({ page }, testInfo) => {
  const { state, requests } = await enterpriseApi(page);
  const raw = '<img src=x onerror="window.__traceUnsafe=true">' + '超长原始内容'.repeat(160);
  state.run.events = [
    { event_id: 1, run_id: state.run.run_id, type: 'node.started', created_at: completed.created_at, payload: { node: 'sql', message: '准备受控查询。'.repeat(30) } },
    { event_id: 2, run_id: state.run.run_id, type: 'new.event.kind', created_at: completed.created_at, payload: { raw, untouched: [1, null, false], lines: Array.from({ length: 100 }, (_, index) => `原始记录 ${index}`) } },
  ];
  await page.goto('/');
  await page.locator('.recent-run').first().click();
  await expect(page.locator('.trace-event')).toHaveCount(0);
  await expect(page.locator('.result-section')).not.toContainText(raw);
  await processButton(page).click();
  const dialog = processDialog(page);
  await expect(dialog).toBeVisible();
  expect((await dialog.boundingBox()).width).toBe(480);
  await expect(dialog.locator('.trace-event')).toHaveCount(2);
  const summaries = await dialog.locator('.trace-event-summary').allTextContents();
  expect(summaries.length).toBe(2);
  for (const text of summaries) expect(Array.from(text).length).toBeLessThanOrEqual(80);
  expect(summaries[1]).not.toContain(raw);
  const event = dialog.locator('.trace-event').last();
  await event.locator('summary').click();
  const payload = event.locator('pre');
  expect(JSON.parse(await payload.innerText())).toEqual(state.run.events[1].payload);
  await expect(event.locator('img')).toHaveCount(0);
  expect(await page.evaluate(() => window.__traceUnsafe)).toBeUndefined();
  const scrolling = await payload.evaluate(node => ({ scrollHeight: node.scrollHeight, height: node.clientHeight, overflow: getComputedStyle(node).overflowY }));
  expect(scrolling.scrollHeight).toBeGreaterThan(scrolling.height);
  expect(scrolling.overflow).toBe('auto');
  await page.getByRole('button', { name: '关闭分析过程', exact: true }).focus();
  await page.keyboard.press('Shift+Tab');
  expect(await dialog.evaluate(node => node.contains(document.activeElement))).toBe(true);
  for (let i = 0; i < 8; i++) {
    await page.keyboard.press('Tab');
    expect(await dialog.evaluate(node => node.contains(document.activeElement))).toBe(true);
  }
  await page.locator('.chat-brand .brand-icon').evaluate(image => image.decode());
  await expect(page.locator('.chart-canvas canvas')).toHaveCount(3);
  await page.screenshot({ path: testInfo.outputPath('trace-desktop-escaped-payload.png') });
  await page.keyboard.press('Escape');
  await expect(dialog).toHaveCount(0);
  await expect(processButton(page)).toBeFocused();
  await processButton(page).click();
  await page.mouse.click(10, 120);
  await expect(dialog).toHaveCount(0);
  await expect(processButton(page)).toBeFocused();
  expect(nonReads(requests)).toEqual([]);
});

test('mobile drawer is full width and changing a task closes its previous process', async ({ page }, testInfo) => {
  const { state, requests } = await enterpriseApi(page);
  state.additionalRuns = [{ ...structuredClone(completed), run_id: 'another-run', question: '另一条独立分析' }];
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');
  await page.getByRole('button', { name: '展开历史对话' }).click();
  await page.locator('.recent-run').first().click();
  await processButton(page).click();
  expect((await processDialog(page).boundingBox()).width).toBe(390);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(390);
  await page.screenshot({ path: testInfo.outputPath('trace-mobile.png') });
  // An external task selection must also close the overlay; do not infer that
  // backdrop-obscured history is directly clickable during normal interaction.
  await page.locator('.recent-run').nth(1).evaluate(button => button.click());
  await expect(processDialog(page)).toHaveCount(0);
  await expect(page.locator('.question-card h1')).toHaveText('另一条独立分析');
  expect(nonReads(requests)).toEqual([]);
});

for (const [status, label, partial] of [
  ['failed', '未完成', false], ['cancelled', '已取消', false],
  ['interrupted', '已中断', false], ['completed', '部分结果 · 待复核', true],
]) {
  test(`known status ${status}${partial ? '-partial' : ''} is shown without inventing missing execution events`, async ({ page }) => {
    const { state, requests } = await enterpriseApi(page);
    state.run.status = status;
    state.run.events = [];
    state.run.artifacts.partial = partial;
    if (status === 'failed') state.run.error = '测试查询失败，不能展示为成功。';
    await page.goto('/');
    await page.locator('.recent-run').first().click();
    await expect(page.locator('.run-status')).toHaveText(label);
    await expect(page.locator('.progress-stages')).toHaveCount(0);
    await processButton(page).click();
    await expect(processDialog(page).locator('.trace-event')).toHaveCount(0);
    await expect(processDialog(page)).not.toContainText('100%');
    expect(nonReads(requests)).toEqual([]);
  });
}

test('repair events reenter querying and drawer toggles preserve SSE deduplication and cursor', async ({ page }) => {
  const { state, requests } = await enterpriseApi(page);
  const event = (id, node) => ({ event_id: id, type: 'node.started', run_id: state.run.run_id, created_at: completed.created_at, payload: { node } });
  state.run.status = 'running';
  state.run.artifacts = {};
  state.run.events = [event(1, 'sql'), event(2, 'analysis')];
  let streamCalls = 0;
  await page.route('**/api/v1/runs/*/events?*', route => {
    streamCalls += 1;
    const repair = event(3, 'repair');
    state.run.events = [...state.run.events, repair];
    return route.fulfill({ status: 200, contentType: 'text/event-stream', body: `id: 3\nevent: node.started\ndata: ${JSON.stringify(repair)}\n\nid: 3\nevent: node.started\ndata: ${JSON.stringify(repair)}\n\nevent: stream.closed\ndata: {}\n\n` });
  });
  await page.goto('/');
  await page.locator('.recent-run').first().click();
  await expect(page.locator('.progress-stage[aria-current="step"]')).toContainText('查询数据');
  await expect(page.locator('.progress-stage')).toHaveCount(4);
  const cursor = await page.evaluate(() => JSON.parse(localStorage.getItem('insight-agents.workspace.v1'))?.after);
  await processButton(page).click();
  await expect(processDialog(page).locator('.trace-event')).toHaveCount(3);
  await page.keyboard.press('Escape');
  await processButton(page).click();
  await expect(processDialog(page).locator('.trace-event')).toHaveCount(3);
  await page.keyboard.press('Escape');
  expect(streamCalls).toBe(1);
  expect(await page.evaluate(() => JSON.parse(localStorage.getItem('insight-agents.workspace.v1'))?.after)).toBe(cursor);
  expect(nonReads(requests)).toEqual([]);
});
