import type { Question, RunEvent } from './api';

function truncate(text: string, limit: number): string {
  const chars = Array.from(text);
  return chars.length > limit ? `${chars.slice(0, limit - 1).join('')}…` : text;
}

export function shortQuestionTitle(question: string, catalog: Question[]): string {
  const matched = catalog.find(item => item.question === question && item.title?.trim());
  if (matched?.title) return matched.title;
  const chars = Array.from(question);
  return chars.length > 24 ? `${chars.slice(0, 24).join('')}…` : question;
}

const NODE_LABELS: Record<string, string> = {
  prepare: '准备上下文', intent: '理解分析需求', discovery: '定位指标与数据',
  clarify: '确认统计口径', investigate: '制定分析步骤', sql: '生成查询',
  execute: '执行查询', review: '校验查询结果', repair: '修复查询',
  analysis: '分析结果', supplement: '检查补查需求', visualization: '生成图表',
  final_review: '复核分析结论', delivery_revision: '调整交付结果',
};
const EVENT_LABELS: Record<string, string> = {
  'run.started': '分析已开始', 'node.started': '开始执行', 'node.completed': '步骤完成',
  'skill.loaded': '加载分析方法', 'memory.recalled': '检索相关经验',
  'preference.applied': '应用分析偏好', 'analysis.tool_used': '执行分析计算',
  'run.input_required': '等待补充信息', 'artifact.created': '生成分析产物',
  'sql.draft_created': '生成查询草稿', 'sql.draft.created': '生成查询草稿',
  'run.completed': '分析已结束', 'run.failed': '分析失败',
  'run.cancelled': '分析已取消', 'run.interrupted': '分析已中断',
};
const ARTIFACT_LABELS: Record<string, string> = {
  discovery: '数据定位结果', investigation: '分析步骤', sql_plan: '查询计划',
  queries: '查询结果', analysis: '分析结论', charts: '可视化图表',
};
function textField(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() ? value.trim().replace(/\s+/g, ' ') : undefined;
}
function labelFor(labels: Record<string, string>, key: string | undefined): string | undefined {
  return key && Object.hasOwn(labels, key) ? labels[key] : undefined;
}

export function eventPresentation(event: RunEvent): { label: string; summary: string } {
  const payload = event.payload || {};
  const label = labelFor(EVENT_LABELS, event.type);
  if (!label) return { label: '其他事件', summary: '已记录此事件，展开查看原始数据。' };
  let summary = textField(payload.message) || textField(payload.question);
  if (!summary && (event.type === 'node.started' || event.type === 'node.completed')) {
    const node = textField(payload.node) || textField(payload.node_name);
    summary = labelFor(NODE_LABELS, node) || '步骤记录已保存，展开查看原始数据。';
  }
  if (!summary && event.type === 'artifact.created') {
    const artifact = textField(payload.artifact);
    const artifactLabel = labelFor(ARTIFACT_LABELS, artifact);
    summary = artifactLabel ? `${artifactLabel}已保存。` : '产物记录已保存，展开查看原始数据。';
  }
  if (!summary && event.type === 'memory.recalled') {
    const count = Array.isArray(payload.ids) ? payload.ids.length : undefined;
    summary = count === undefined ? '检索记录已保存，是否应用以后续执行记录为准。' : `检索到 ${count} 条相关记录，是否应用以后续执行记录为准。`;
  }
  if (!summary && event.type === 'skill.loaded') summary = textField(payload.skill_name) || textField(payload.skill_id) || '分析方法已加载。';
  if (!summary && event.type === 'analysis.tool_used') summary = textField(payload.tool) || '计算工具调用已记录。';
  if (!summary && event.type === 'run.started' && payload.resumed === true) summary = '收到补充信息，继续本次分析。';
  return { label, summary: truncate(summary || '执行记录已保存，展开查看原始数据。', 80) };
}

export function orderedRunEvents(events: RunEvent[]): RunEvent[] {
  const unique = new Map<number, RunEvent>();
  for (const event of events) {
    const id = Number(event.event_id);
    if (Number.isSafeInteger(id) && id >= 0) unique.set(id, event);
  }
  return [...unique.values()].sort((a, b) => Number(a.event_id) - Number(b.event_id));
}

export type ProgressState = 'pending' | 'active' | 'complete' | 'paused' | 'failed' | 'cancelled' | 'partial';
export interface ProgressStage { label: string; state: ProgressState }
export interface ProgressPresentation { label: string; tone: ProgressState; stages: ProgressStage[] }

const NODE_STAGE: Record<string, number> = {
  prepare: 0, intent: 0, discovery: 0, clarify: 0, investigate: 0,
  sql: 1, execute: 1, review: 1, repair: 1,
  analysis: 2, supplement: 2,
  visualization: 3, final_review: 3, delivery_revision: 3,
};
const STAGE_LABELS = ['理解需求', '查询数据', '结果分析', '图表与复核'];
const COMPLETE_STATUSES = new Set(['completed', 'succeeded', 'success']);

export function deriveProgress(events: RunEvent[], status: string, partial = false): ProgressPresentation {
  let tone: ProgressState = 'pending';
  let label = '状态已记录';
  if (status === 'running') { tone = 'active'; label = '正在分析'; }
  else if (status === 'queued' || status === 'pending') label = '等待执行';
  else if (status === 'waiting_for_input') { tone = 'paused'; label = '等待补充信息'; }
  else if (status === 'failed') { tone = 'failed'; label = '分析失败'; }
  else if (status === 'cancelled') { tone = 'cancelled'; label = '已取消'; }
  else if (status === 'interrupted') { tone = 'paused'; label = '已中断'; }
  else if (COMPLETE_STATUSES.has(status)) {
    tone = partial ? 'partial' : 'complete';
    label = partial ? '部分结果 · 待复核' : '分析已完成';
  }
  if (partial && !COMPLETE_STATUSES.has(status)) label += ' · 已有部分结果';

  const stages: ProgressStage[] = STAGE_LABELS.map(label => ({ label, state: 'pending' }));
  let current: number | undefined;
  let latestCompleted = false;
  for (const event of orderedRunEvents(events)) {
    if (event.type !== 'node.started' && event.type !== 'node.completed') continue;
    const node = textField(event.payload?.node) || textField(event.payload?.node_name);
    const index = node && Object.hasOwn(NODE_STAGE, node) ? NODE_STAGE[node] : undefined;
    if (index === undefined) continue;
    // Returning to discovery / SQL invalidates the downstream phase indicators.
    if (current !== undefined && index < current) {
      for (let next = index + 1; next < stages.length; next += 1) stages[next].state = 'pending';
    }
    if (current !== undefined && current !== index && stages[current].state === 'active') {
      stages[current].state = latestCompleted ? 'complete' : 'pending';
    }
    current = index;
    latestCompleted = event.type === 'node.completed';
    stages[index].state = latestCompleted ? 'complete' : 'active';
  }
  // A terminal run event alone does not prove which stages were executed.
  if (current === undefined) return { label, tone, stages: [] };
  if (tone === 'complete') {
    if (!latestCompleted) stages[current].state = 'pending';
  } else if (tone !== 'pending') stages[current].state = tone;
  return { label, tone, stages };
}
