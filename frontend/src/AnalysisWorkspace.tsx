import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { API, ApiError, api, errorText, printable } from './api';
import type { Health, Memory, Question, Run, RunEvent, Scenario, Skill } from './api';
import ChartView from './LazyChart';
import DataTable from './DataTable';
import { CalculationPanels, FindingLabel, InvestigationPanel } from './EvidenceDetails';
import { lastEventCursor, mergeRunEvents } from './events';
import { BRAND_NAME, BrandIcon } from './Brand';
import { shortQuestionTitle } from './presentation';
import { AnalysisProgress, TraceDrawer } from './TracePanel';

const STORAGE_KEY = 'insight-agents.workspace.v1';
const RUNNING = new Set(['queued', 'running', 'pending']);
const DONE = new Set(['completed', 'succeeded', 'success']);
const EVENT_TYPES = ['run.started', 'node.started', 'node.completed', 'skill.loaded', 'memory.recalled', 'preference.applied', 'analysis.tool_used', 'run.input_required', 'artifact.created', 'run.completed', 'run.failed', 'run.cancelled'];
export const STATUS_LABELS: Record<string, string> = { queued: '等待执行', running: '正在分析', waiting_for_input: '等待补充', completed: '已完成', succeeded: '已完成', success: '已完成', failed: '未完成', cancelled: '已取消', interrupted: '已中断', pending: '等待执行' };
export function runStatusLabel(status: string, partial = false) { return partial && DONE.has(status) ? '部分结果 · 待复核' : STATUS_LABELS[status] || status; }
interface SavedSession { scenarioId: string; mode?: string; runId?: string; threadId?: string; after?: number }
function readSession(): SavedSession | null { try { return JSON.parse(localStorage.getItem(STORAGE_KEY) || 'null'); } catch { return null; } }
export function Icon({ name, size = 20 }: { name: string; size?: number }) {
  const paths: Record<string, string> = {
    search: 'M10.5 18a7.5 7.5 0 110-15 7.5 7.5 0 010 15 M16 16l5 5',
    panel: 'M3 4h18v16H3z M9 4v16',
    grid: 'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z',
    bag: 'M5 7h14l1 14H4L5 7z M8 8V6a4 4 0 018 0v2',
    store: 'M3 10V5h18v5 M4 10v11h16V10 M2 10h20 M9 21v-7h6v7',
    plus: 'M12 5v14 M5 12h14', arrow: 'M5 12h14 M13 6l6 6-6 6',
    send: 'M22 2L9 15 M22 2l-7 20-6-7-7-6 20-7z', spark: 'M12 3l2.7 6.3L21 12l-6.3 2.7L12 21l-2.7-6.3L3 12l6.3-2.7L12 3z',
    book: 'M4 4h7a3 3 0 013 3v14a4 4 0 00-3-2H4V4z M14 7a3 3 0 013-3h4v15h-4a4 4 0 00-3 2',
    clock: 'M12 8v5l3 2 M21 12a9 9 0 11-18 0 9 9 0 0118 0',
    chevron: 'M9 5l7 7-7 7', close: 'M6 6l12 12 M18 6L6 18', check: 'M5 12l4 4L19 6',
    chart: 'M4 4v16h17 M8 15v-4 M13 15V7 M18 15V4',
    code: 'M8 6l-6 6 6 6 M16 6l6 6-6 6 M14 3l-4 18',
    list: 'M9 5h12 M9 12h12 M9 19h12 M3 5h1 M3 12h1 M3 19h1',
    stop: 'M6 6h12v12H6z', refresh: 'M20 7a8 8 0 10.8 8 M20 3v5h-5',
    info: 'M12 11v6 M12 7v.1 M21 12a9 9 0 11-18 0 9 9 0 0118 0',
    play: 'M8 4l12 8-12 8V4z', database: 'M20 6c0 2-3.6 3-8 3S4 8 4 6s3.6-3 8-3 8 1 8 3z M4 6v12c0 2 3.6 3 8 3s8-1 8-3V6 M4 12c0 2 3.6 3 8 3s8-1 8-3',
    edit: 'M4 16L16 4l4 4L8 20H4v-4z M14 6l4 4', trash: 'M3 6h18 M9 6V3h6v3 M5 6l1 15h12l1-15 M10 10v7 M14 10v7',
    layers: 'M12 3l10 5-10 5L2 8l10-5z M2 12l10 5 10-5 M2 16l10 5 10-5',
  };
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={paths[name] || paths.spark} /></svg>;
}

function QuestionBubble({ question, createdAt, referenceTime }: { question: string; createdAt: string; referenceTime: string }) {
  const [expanded, setExpanded] = useState(false);
  const [overflows, setOverflows] = useState(false);
  const text = useRef<HTMLHeadingElement>(null);
  const id = useId();
  useLayoutEffect(() => {
    const element = text.current;
    if (!element) return;
    const measure = () => {
      if (!element.clientWidth) return;
      const style = getComputedStyle(element);
      const lineHeight = Number.parseFloat(style.lineHeight);
      const padding = Number.parseFloat(style.paddingTop) + Number.parseFloat(style.paddingBottom);
      setOverflows(element.scrollHeight > lineHeight * 4 + padding + 1);
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [question]);
  return <div className="question-card"><div className="question-content">
    <div className="question-bubble"><h1 ref={text} id={id} className={'question-text' + (expanded ? '' : ' is-collapsed')}>{question}</h1></div>
    {overflows && <button className="question-expand" aria-expanded={expanded} aria-controls={id} onClick={() => setExpanded(value => !value)}>{expanded ? '收起' : '展开全文'}</button>}
    <p><time dateTime={createdAt}>{referenceTime}</time></p>
  </div><span className="question-avatar" aria-hidden="true">我</span></div>;
}


interface WorkspaceProps {
  domain: Scenario;
  externalRunId?: string;
  resetKey?: number;
  onRunConsumed: () => void;
  onRunChange: (run: Run | null) => void;
}
export default function AnalysisWorkspace({ domain, externalRunId, resetKey, onRunConsumed, onRunChange }: WorkspaceProps) {
  const saved = useRef(readSession());
  const [health, setHealth] = useState<Health | null>(null);
  const [run, setRun] = useState<Run | null>(null);
  const [threadId, setThreadId] = useState<string>();
  const [questions, setQuestions] = useState<Question[]>([]);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [question, setQuestion] = useState('');
  const [answer, setAnswer] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [booting, setBooting] = useState(true);
  const [connection, setConnection] = useState('');
  const [traceRunId, setTraceRunId] = useState<string | null>(null);
  const closeTrace = useCallback(() => setTraceRunId(null), []);
  const cursor = useRef(0);
  const actionSequence = useRef(0);
  const stream = useRef<EventSource | null>(null);
  const initialized = useRef(false);
  const priorReset = useRef(resetKey);
  const composer = useRef<HTMLTextAreaElement>(null);
  const selected = domain.id;
  const running = !!run && RUNNING.has(run.status);
  const waiting = run?.status === 'waiting_for_input';
  const ready = !!health?.postgres_ready && !!health?.llm_configured && !!domain.model_version;
  useEffect(() => { setTraceRunId(null); }, [run?.run_id]);
  useLayoutEffect(() => {
    const textarea = composer.current;
    if (!textarea) return;
    const resize = () => {
      if (!textarea.clientWidth) return;
      textarea.style.height = '0px';
      const height = Math.max(run ? 30 : 78, Math.min(textarea.scrollHeight, 166));
      textarea.style.height = `${height}px`;
      textarea.style.overflowY = textarea.scrollHeight > height ? 'auto' : 'hidden';
    };
    resize();
    let lastWidth = textarea.clientWidth;
    const observer = new ResizeObserver(() => {
      if (textarea.clientWidth !== lastWidth) { lastWidth = textarea.clientWidth; resize(); }
    });
    observer.observe(textarea);
    return () => observer.disconnect();
  }, [question, run?.run_id]);
  const loadHealth = useCallback(async () => { try { setHealth(await api<Health>('/health')); } catch { setHealth(null); } }, []);
  const mergeEvents = useCallback((incoming: RunEvent[]) => {
    if (!incoming?.length) return;
    cursor.current = Math.max(cursor.current, lastEventCursor(incoming));
    setEvents(current => mergeRunEvents(current, incoming));
  }, []);
  const adoptRun = useCallback((next: Run, replaceEvents = false) => {
    setRun(next); setThreadId(next.thread_id || undefined);
    if (replaceEvents) { cursor.current = 0; setEvents([]); }
    mergeEvents(next.events || []);
  }, [mergeEvents]);
  function resetWork() {
    actionSequence.current += 1; stream.current?.close(); setRun(null); setThreadId(undefined);
    setEvents([]); cursor.current = 0; setError(''); setQuestion('');
    setAnswer(''); setConnection(''); setBusy(false); setTraceRunId(null);
    try { localStorage.removeItem(STORAGE_KEY); } catch { /* Optional local state. */ }
  }
  async function openRun(id: string) {
    resetWork(); const token = actionSequence.current; setBusy(true);
    try {
      const next = await api<Run>('/runs/' + encodeURIComponent(id));
      if (token !== actionSequence.current) return;
      if ((next.domain_id || next.scenario_id) !== selected) throw new Error('该记录属于其他业务域，请从最近分析重新打开。');
      adoptRun(next, true);
    } catch (err) { if (token === actionSequence.current) setError(err instanceof ApiError && err.status === 404 ? '这条分析记录已删除或不存在，请从左侧选择其他记录。' : '无法读取分析记录：' + errorText(err) + '。没有重新提交任务。'); }
    finally { if (token === actionSequence.current) setBusy(false); }
  }
  useEffect(() => {
    let active = true;
    void loadHealth().finally(() => {
      if (!active) return;
      setBooting(false);
      if (!initialized.current) {
        initialized.current = true;
        const initial = saved.current;
        if (!externalRunId && initial?.scenarioId === selected && initial.mode !== 'replay' && initial.runId) void openRun(initial.runId);
      }
    });
    api<Question[]>('/questions').then(value => { if (active) setQuestions(value.filter(item => item.domain_id === selected)); }).catch(() => { if (active) setQuestions([]); });
    return () => { active = false; actionSequence.current += 1; stream.current?.close(); };
  }, [selected, loadHealth]);
  useEffect(() => {
    if (!externalRunId) return;
    void openRun(externalRunId); onRunConsumed();
  }, [externalRunId]);
  useEffect(() => {
    if (priorReset.current === resetKey) return;
    priorReset.current = resetKey; resetWork(); composer.current?.focus();
  }, [resetKey]);
  useEffect(() => { onRunChange(run); }, [run?.run_id, run?.status, onRunChange]);
  useEffect(() => {
    if (booting || busy || !run) return;
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify({ scenarioId: selected, mode: 'live', runId: run.run_id, threadId, after: cursor.current })); } catch { /* Never retry a task for local storage. */ }
  }, [selected, run?.run_id, threadId, events, booting, busy]);

  useEffect(() => {
    if (!run || !RUNNING.has(run.status)) return;
    const id = run.run_id;
    let active = true;
    let refreshTimer: ReturnType<typeof setTimeout> | undefined;
    let refreshing = false; let refreshAgain = false;
    const source = new EventSource(API + '/runs/' + encodeURIComponent(id) + '/events?after=' + cursor.current);
    stream.current = source; setConnection('正在连接执行记录…');
    const refresh = async () => {
      if (refreshing) { refreshAgain = true; return; } refreshing = true;
      try { const detail = await api<Run>('/runs/' + encodeURIComponent(id)); if (active) adoptRun(detail); }
      catch (err) { if (active) setError(errorText(err)); }
      finally { refreshing = false; if (refreshAgain && active) { refreshAgain = false; void refresh(); } }
    };
    const receive = (message: MessageEvent<string>) => {
      try {
        const event = JSON.parse(message.data) as RunEvent;
        if (!event.event_id && message.lastEventId) event.event_id = Number(message.lastEventId);
        mergeEvents([event]);
        if (['artifact.created', 'run.input_required', 'run.completed', 'run.failed', 'run.cancelled'].includes(event.type)) { clearTimeout(refreshTimer); refreshTimer = setTimeout(() => void refresh(), 100); }
      } catch { setConnection('收到无法解析的事件，等待后续更新'); }
    };
    EVENT_TYPES.forEach(type => source.addEventListener(type, receive as EventListener));
    source.onmessage = receive;
    source.onopen = () => setConnection('执行记录已连接');
    source.onerror = () => { if (active) setConnection('连接暂时中断，正在从事件游标重连…'); };
    source.addEventListener('stream.closed', () => { source.close(); setConnection('执行记录已同步'); void refresh(); });
    return () => { active = false; clearTimeout(refreshTimer); source.close(); if (stream.current === source) stream.current = null; };
  }, [run?.run_id, run?.status, mergeEvents, adoptRun]);

  async function submit(event: FormEvent) {
    event.preventDefault(); const text = question.trim();
    if (!text || busy || running || waiting || !ready) return;
    const token = ++actionSequence.current; setBusy(true); setError('');
    try {
      const result = await api<Run>('/runs', { method: 'POST', body: JSON.stringify({ domain_id: selected, question: text, ...(threadId ? { thread_id: threadId } : {}) }) });
      if (token !== actionSequence.current) return;
      adoptRun(result, true); setQuestion('');
    } catch (err) { if (token === actionSequence.current) setError(errorText(err)); }
    finally { if (token === actionSequence.current) setBusy(false); }
  }
  async function resume(event: FormEvent) {
    event.preventDefault(); if (!run || !answer.trim() || busy) return;
    const token = ++actionSequence.current; setBusy(true); setError('');
    try { const next = await api<Run>('/runs/' + encodeURIComponent(run.run_id) + '/resume', { method: 'POST', body: JSON.stringify({ answer: answer.trim() }) }); if (token === actionSequence.current) { adoptRun({ ...run, ...next }); setAnswer(''); } }
    catch (err) { if (token === actionSequence.current) setError(errorText(err)); }
    finally { if (token === actionSequence.current) setBusy(false); }
  }
  async function cancel() {
    if (!run) return; const token = ++actionSequence.current; setBusy(true); setError('');
    try { await api('/runs/' + encodeURIComponent(run.run_id), { method: 'DELETE' }); const next = await api<Run>('/runs/' + encodeURIComponent(run.run_id)); if (token === actionSequence.current) adoptRun(next); }
    catch (err) { if (token === actionSequence.current) setError(errorText(err)); }
    finally { if (token === actionSequence.current) setBusy(false); }
  }
  const clarificationEvent = [...events].reverse().find(event => event.type === 'run.input_required');
  const clarification = clarificationEvent?.payload.question ?? (typeof run?.artifacts?.clarification === 'string' ? run.artifacts.clarification : run?.artifacts?.clarification?.question) ?? '请补充指标口径或查询条件。';
  const analysis = run?.artifacts?.analysis;
  const queries = run?.artifacts?.queries || [];
  const charts = run?.artifacts?.charts || [];
  const partial = !!run?.artifacts?.partial;
  const unavailable = !health ? '暂未连接分析服务' : !health.postgres_ready ? 'PostgreSQL 未就绪，暂不能提交分析' : !health.llm_configured ? '模型尚未配置，暂不能提交分析' : !domain.model_version ? '请先在“数据与语义”发布语义模型' : '';
  const referenceTime = run?.created_at ? new Date(run.created_at).toLocaleString('zh-CN', { hour12: false }) : '';
  const composerForm = <div className="composer-area">
    <form className={'composer ' + (run ? 'composer-result' : 'composer-home') + (!ready ? ' disabled' : '')} onSubmit={submit}>
      <textarea ref={composer} aria-label="分析问题" placeholder={waiting ? '请先补充上方信息…' : !ready ? unavailable : run ? '继续追问，或进一步查看某个维度…' : '描述你想分析的问题，让数据给你答案…'} value={question} onChange={event => setQuestion(event.target.value)} disabled={!ready || busy || running || waiting} rows={1} onKeyDown={event => {
        if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing && event.nativeEvent.keyCode !== 229) {
          event.preventDefault(); event.currentTarget.form?.requestSubmit();
        }
      }} />
      <div className="composer-bottom">
        <span className="composer-domain" title={domain.title}><Icon name="database" size={16} />{domain.title}</span>
        {running || waiting ? <button className="cancel-button" type="button" onClick={() => void cancel()} disabled={busy}><Icon name="stop" size={14} />取消任务</button> : <button className="send-button" type="submit" aria-label="发送分析问题" title="发送分析问题" disabled={!ready || busy || !question.trim()}>{busy ? <span className="spinner tiny" /> : <Icon name="send" size={18} />}</button>}
      </div>
    </form>
    <p className="composer-caption"><span className="data-scope"><Icon name="info" size={14} />只读分析{!run && domain.date_range ? ' · ' + domain.date_range.start + ' — ' + domain.date_range.end : ''}</span><span>Enter 发送 · Shift + Enter 换行</span></p>
  </div>;
  return <div className={'analysis-embedded bi-analysis ' + (run ? 'conversation-active' : 'conversation-home')}>
    {run && <header className="analysis-toolbar"><div className="analysis-toolbar-inner"><div><strong title={run.question}>{shortQuestionTitle(run.question, questions)}</strong><span>{domain.title}</span></div><time dateTime={run.created_at}>{referenceTime}</time></div></header>}
    <div className="analysis-scroll">
      {!ready && !booting && <div className="mode-notice warning-notice"><Icon name="info" size={16} />{unavailable}。已保存的结果仍可查看。</div>}
      {error && <div className="error-banner" role="alert"><Icon name="info" size={18} /><span>{error}</span><button className="icon-button" aria-label="关闭错误" onClick={() => setError('')}><Icon name="close" size={15} /></button></div>}
      {busy && !run && <div className="loading-state"><span className="spinner" />正在读取分析记录…</div>}
      {!run && <section className="analysis-welcome">
        <div className="welcome-heading"><span className="welcome-icon"><BrandIcon decorative={false} /></span><h1>今天，想分析什么？</h1><p>提出问题，让数据给你答案。</p></div>
        {composerForm}
        {questions.length > 0 && <div className="welcome-suggestions"><div className="section-heading"><h2>试着这样问</h2><span>点击填入问题</span></div><div className="question-catalog">{questions.map((item, index) => <button key={item.id} disabled={busy} onClick={() => { setQuestion(item.question); composer.current?.focus(); }}><span className={'suggestion-icon tone-' + index % 4}><Icon name={['chart', 'layers', 'bag', 'spark'][index % 4]} size={22} /></span><span><strong>{item.title || item.question}</strong></span><Icon name="chevron" size={16} /></button>)}</div></div>}
      </section>}
      {run && <section className="result-section">
        <QuestionBubble key={run.run_id + run.question} question={run.question} createdAt={run.created_at} referenceTime={referenceTime} />
        <div className="assistant-response"><div className="assistant-identity"><span className="chat-logo"><BrandIcon /></span><strong>{BRAND_NAME}</strong><span className={'run-status ' + (partial && DONE.has(run.status) ? 'partial' : DONE.has(run.status) ? 'done' : running ? 'working' : '')}>{running && <span className="spinner tiny" />}{runStatusLabel(run.status, partial)}</span></div>
        {run.error && <div className="error-banner" role="alert">{printable(run.error)}</div>}
        {partial && <div className="mode-notice warning-notice partial-result-notice" role="status">当前仅保留部分结果，结论尚需复核，不应视为完整分析成功。已保存的数据与执行记录仍可查看。</div>}
        {run.status === 'interrupted' && <div className="mode-notice warning-notice">任务已中断，未重新提交。你可查看已有结果后决定是否开启新分析。</div>}
        <AnalysisProgress events={events} status={run.status} partial={partial} />
        {waiting && <form className="clarification-card" onSubmit={resume}><div><Icon name="info" size={19} /><strong>确认一个细节，就可以继续</strong></div><p>{printable(clarification)}</p><label htmlFor="clarification">补充说明</label><textarea id="clarification" placeholder="请填写你的统计口径或补充条件…" value={answer} onChange={event => setAnswer(event.target.value)} rows={2} required /><div className="clarification-actions"><span>保留已完成的分析，补充后继续</span><button className="primary-button" disabled={busy || !answer.trim()} type="submit">补充并继续<Icon name="arrow" size={15} /></button></div></form>}
        {analysis?.summary && <section className="answer-card"><div className="answer-heading"><span><Icon name="spark" size={18} />分析结论</span>{run.artifacts?.partial && <span className="partial-badge">部分结果</span>}</div><p className="answer-summary">{analysis.summary}</p>{!!analysis.findings?.length && <details className="answer-next findings-detail"><summary>关键发现与解释 · {analysis.findings.length}</summary><div className="answer-findings">{analysis.findings.map((finding, index) => <article key={index}><h3><FindingLabel kind={finding.kind} />{finding.title}</h3><p>{finding.detail}</p>{!!finding.business_objects?.length && <div className="business-object-tags">{finding.business_objects.map(item => <span key={item}>{item}</span>)}</div>}</article>)}</div></details>}{!!analysis.recommendations?.length && <details className="answer-next"><summary>建议与后续行动 · {analysis.recommendations.length}</summary><ul>{analysis.recommendations.map((item, index) => <li key={index}>{item}</li>)}</ul></details>}</section>}
        {!analysis && running && <div className="thinking-card"><span className="spinner" /><div><strong>正在分析数据</strong><p>结果会随执行进度更新，详细步骤可在“查看分析过程”中查看。</p></div></div>}
        {!analysis && !running && !waiting && <div className="empty-state compact">此任务没有返回分析结论。可查看已有数据与执行记录。</div>}
        {!!charts.length && <div className="charts-grid">{charts.map((chart, index) => { const query = queries.find(item => item.id === chart.query_id); return <article className={'chart-card chart-' + chart.type} key={chart.query_id + '-' + index}><div className="chart-heading"><h2>{chart.title}</h2></div>{query ? <ChartView chart={chart} query={query} /> : <div className="empty-state compact">图表引用的结果尚未返回。</div>}</article>; })}</div>}
        {!!queries.length && <section className="result-data-section"><div className="section-heading"><h2>数据结果</h2><span>保存的查询与计算结果</span></div>{queries.map(query => <details className="saved-query" key={query.id} open={queries.length === 1}><summary><span><Icon name="database" size={15} /><strong>{query.title || charts.find(chart => chart.query_id === query.id)?.title || query.id}</strong><code>{query.id}</code></span><span>{query.total_rows ?? query.rows.length} 行{query.derived ? ' · 计算结果' : ''}</span></summary><DataTable query={query} runId={run.run_id} /></details>)}</section>}
        <details className="evidence-panel"><summary><span><Icon name="code" size={16} />分析依据与查询 SQL</span><span>口径 · 计算方法 · 边界</span></summary><div className="evidence-meta"><span>{domain.title}</span><span>语义版本 {run.model_version ?? '未记录'}</span><span>生成于 {referenceTime}</span></div><InvestigationPanel investigation={run.artifacts?.investigation} /><CalculationPanels calculations={run.artifacts?.calculations} />{run.artifacts?.discovery && <details className="discovery-details"><summary>指标口径与数据定位</summary><pre>{JSON.stringify(run.artifacts.discovery, null, 2)}</pre></details>}{queries.map(query => <article className="query-card" key={query.id}><div className="query-header"><h3>{query.id}</h3><span className="readonly-badge">{query.derived ? '确定性计算产物' : '只读查询'}</span></div><pre className="sql-code"><code>{query.sql || '此结果由分析工具计算生成，计算方法见上方记录。'}</code></pre></article>)}{!!analysis?.evidence_gaps?.length && <div className="limitations"><strong>尚缺证据</strong><ul>{analysis.evidence_gaps.map((item, index) => <li key={index}>{item}</li>)}</ul></div>}{!!analysis?.limitations?.length && <div className="limitations"><strong>分析边界</strong><ul>{analysis.limitations.map((item, index) => <li key={index}>{item}</li>)}</ul></div>}</details>
        <button className="trace-trigger" aria-label="查看分析过程" aria-haspopup="dialog" onClick={() => setTraceRunId(run.run_id)}><span><Icon name="list" size={16} />查看分析过程<span className="count-tag">{events.length}</span></span><span>{connection || (running ? '进行中' : '已保存')}<Icon name="chevron" size={15} /></span></button>
        {analysis?.follow_up_question && <button className="followup-suggestion" onClick={() => { setQuestion(analysis.follow_up_question || ''); composer.current?.focus(); }}><Icon name="arrow" size={15} />{analysis.follow_up_question}</button>}
        </div>
      </section>}
    </div>
    {run && composerForm}
    {run && traceRunId === run.run_id && <TraceDrawer events={events} status={run.status} partial={partial} connection={connection} onClose={closeTrace} />}
  </div>;
}

export function ResourceDrawer({ scenarioId, scenarioTitle, section, onSection, onClose }: { scenarioId: string; scenarioTitle: string; section: 'memories' | 'skills'; onSection: (section: 'memories' | 'skills') => void; onClose: () => void }) {
  const [memories, setMemories] = useState<Memory[]>([]);
  const [skills, setSkills] = useState<Skill[]>([]);
  const [filter, setFilter] = useState('all');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [mutating, setMutating] = useState<string | null>(null);
  const [editing, setEditing] = useState<Memory | null>(null);
  const [editValue, setEditValue] = useState('');
  const [editContent, setEditContent] = useState('');
  const closeButton = useRef<HTMLButtonElement>(null);
  const kinds: Record<string, string> = { business: '业务知识', sql_experience: 'SQL 修复经验', preference: '偏好模板' };
  const load = useCallback(async () => {
    setLoading(true); setError('');
    try {
      if (section === 'skills') setSkills(await api<Skill[]>(`/skills?domain_id=${encodeURIComponent(scenarioId)}`));
      else setMemories(await api<Memory[]>(`/memories?domain_id=${encodeURIComponent(scenarioId)}`));
    } catch (err) { setError(errorText(err)); }
    finally { setLoading(false); }
  }, [scenarioId, section]);
  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    const prior = document.activeElement as HTMLElement;
    closeButton.current?.focus();
    function onKey(event: KeyboardEvent) { if (event.key === 'Escape') onClose(); }
    window.addEventListener('keydown', onKey);
    return () => { window.removeEventListener('keydown', onKey); prior?.focus(); };
  }, [onClose]);
  async function update(memory: Memory, patch: { active?: boolean; value?: unknown; content?: string }) {
    setMutating(memory.id); setError('');
    try { const result = await api<Memory>(`/memories/${encodeURIComponent(memory.id)}?domain_id=${encodeURIComponent(scenarioId)}`, { method: 'PATCH', body: JSON.stringify(patch) }); setMemories(current => current.map(item => item.id === memory.id ? result : item)); setEditing(null); }
    catch (err) { setError(errorText(err)); }
    finally { setMutating(null); }
  }
  async function remove(memory: Memory) {
    if (!window.confirm(`删除“${memory.title}”？删除后不再参与检索或偏好应用，内置条目也不会在重启后自动恢复。`)) return;
    setMutating(memory.id); setError('');
    try { await api(`/memories/${encodeURIComponent(memory.id)}?domain_id=${encodeURIComponent(scenarioId)}`, { method: 'DELETE' }); setMemories(current => current.filter(item => item.id !== memory.id)); }
    catch (err) { setError(errorText(err)); }
    finally { setMutating(null); }
  }
  function edit(memory: Memory) {
    setEditing(memory); setEditContent(memory.content || '');
    setEditValue(JSON.stringify(memory.value ?? memory.metadata?.value ?? '', null, 2));
  }
  function saveEdit(event: FormEvent) {
    event.preventDefault(); if (!editing) return;
    if (editing.kind === 'preference') {
      try { const value: unknown = JSON.parse(editValue); void update(editing, { value }); }
      catch { setError('偏好值需为有效 JSON。文字请保留双引号，例如 "按渠道优先展示"。'); }
    } else void update(editing, { content: editContent });
  }
  const filtered = memories.filter(item => filter === 'all' || item.kind === filter);
  return <div className="drawer-backdrop" onClick={onClose}><section className="resource-drawer" role="dialog" aria-modal="true" aria-labelledby="resource-heading" onClick={event => event.stopPropagation()}><div className="drawer-heading"><div><p className="eyebrow">WORKSPACE RESOURCES</p><h2 id="resource-heading">知识、经验与偏好</h2><p>{scenarioTitle} · 知识、经验与偏好严格按业务域隔离</p></div><button ref={closeButton} className="icon-button" aria-label="关闭资源面板" onClick={onClose}><Icon name="close" /></button></div><div className="drawer-tabs"><button className={section === 'memories' ? 'active' : ''} onClick={() => onSection('memories')}><Icon name="book" size={17} />知识与偏好</button><button className={section === 'skills' ? 'active' : ''} onClick={() => onSection('skills')}><Icon name="spark" size={17} />内置 Skills</button><button className="icon-button drawer-refresh" aria-label="刷新资源" onClick={() => void load()}><Icon name="refresh" size={16} /></button></div>{error && <div className="error-banner" role="alert"><Icon name="info" size={18} /><span>{error}</span></div>}<div className="drawer-content">{section === 'memories' ? <><div className="library-note"><Icon name="info" size={16} /><p>内置偏好默认不启用，不代表你的历史偏好。启用后仅在本次要求未明确时提供默认值，不覆盖正式指标定义与安全规则。</p></div><div className="memory-filters">{[['all', '全部'], ['business', '业务知识'], ['sql_experience', '修复经验'], ['preference', '偏好模板']].map(([id, title]) => <button key={id} className={filter === id ? 'active' : ''} onClick={() => setFilter(id)}>{title}<span>{memories.filter(item => id === 'all' || item.kind === id).length}</span></button>)}</div>{filtered.map(memory => <article className={`memory-card ${!memory.active ? 'inactive' : ''}`} key={memory.id}><div className="memory-card-heading"><span className={`memory-kind ${memory.kind}`}>{kinds[memory.kind] || memory.kind}</span><span className="memory-origin">{memory.origin === 'builtin' ? '内置种子' : memory.origin === 'runtime' ? '任务沉淀' : memory.origin || '已保存'}</span><button className={`switch ${memory.active ? 'on' : ''}`} role="switch" aria-checked={memory.active} aria-label={`${memory.active ? '停用' : '启用'}${memory.title}`} disabled={mutating === memory.id} onClick={() => void update(memory, { active: !memory.active })}><span /></button></div><h3>{memory.title}</h3><p>{memory.content || printable(memory.value ?? memory.metadata?.value)}</p>{memory.kind === 'preference' && <div className="preference-value"><code>{printable(memory.key ?? memory.metadata?.key)}</code><span>{printable(memory.value ?? memory.metadata?.value)}</span></div>}<div className="memory-card-footer"><span>{memory.active ? '已启用' : '未启用，不参与应用'} · {memory.id}</span><button className="icon-button" aria-label={`编辑${memory.title}`} disabled={!!mutating} onClick={() => edit(memory)}><Icon name="edit" size={15} /></button><button className="icon-button danger" aria-label={`删除${memory.title}`} disabled={!!mutating} onClick={() => void remove(memory)}><Icon name="trash" size={15} /></button></div></article>)}{!loading && !filtered.length && !error && <div className="empty-state"><Icon name="book" size={30} /><p>当前分类没有记忆条目。</p></div>}</> : <><div className="library-note"><Icon name="info" size={16} /><p>Skill 是节点使用的分析方法与约束，不是额外执行权限。符合触发条件才加载；是否实际使用可在执行轨迹中查看。</p></div>{skills.map(skill => <article className="skill-card" key={skill.id}><div className="skill-icon"><Icon name="spark" size={19} /></div><div><h3>{skill.name || skill.id}<span>{skill.version ? `v${skill.version}` : ''}</span></h3><p>{skill.description}</p><div className="tag-row">{(skill.allowed_nodes || []).map(node => <span key={node}>{node}</span>)}</div></div></article>)}{!loading && !skills.length && !error && <div className="empty-state">当前业务域暂无可用 Skill。</div>}</>}{loading && <div className="loading-state"><span className="spinner" />正在加载资源…</div>}</div>{editing && <form className="memory-editor" onSubmit={saveEdit}><div><h3>编辑 · {editing.title}</h3><button type="button" className="icon-button" aria-label="取消编辑" onClick={() => setEditing(null)}><Icon name="close" size={16} /></button></div><label htmlFor="memory-content">{editing.kind === 'preference' ? '偏好值（JSON）' : '记忆内容'}</label><textarea id="memory-content" value={editing.kind === 'preference' ? editValue : editContent} onChange={event => editing.kind === 'preference' ? setEditValue(event.target.value) : setEditContent(event.target.value)} rows={5} required /><p>编辑偏好不会自动启用。修改内容不能替代业务域的正式指标卡。</p><button className="primary-button" type="submit" disabled={!!mutating}>保存修改</button></form>}</section></div>;
}
