import { useCallback, useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { api, cell, errorText, printable } from './api';
import type { Chart, Dashboard, DashboardCard, DashboardTemplate, Domain, Drilldown, RecordValue, Run } from './api';
import { Icon } from './AnalysisWorkspace';
import ChartView from './LazyChart';
import DataTable from './DataTable';
import { CalculationPanels } from './EvidenceDetails';

const encode = encodeURIComponent;
export function timeLabel(value?: string) { return value ? new Date(value).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : '尚未刷新'; }
export function comparableFilters(filters: RecordValue) { return JSON.stringify(Object.fromEntries(Object.entries(filters || {}).filter(([, value]) => value !== '' && value !== undefined && value !== null).sort(([a], [b]) => a.localeCompare(b)))); }
export function parseFilters(draft: Record<string, string>): RecordValue {
  return Object.fromEntries(Object.entries(draft).filter(([, value]) => value.trim() !== '').map(([key, value]) => [key, value.trim().startsWith('[') ? JSON.parse(value) : value.trim()]));
}
function orderedBoards(boards: Dashboard[], modelVersion: Domain['model_version']) {
  const current = (board: Dashboard) => modelVersion != null && String(board.model_version) === String(modelVersion) ? 1 : 0;
  const updated = (board: Dashboard) => Date.parse(board.updated_at || '') || 0;
  return [...boards].sort((a, b) => current(b) - current(a) || Number(!!b.cards?.length) - Number(!!a.cards?.length) || updated(b) - updated(a));
}

export default function Dashboards({ domain, initialBoardId, onDrilldown }: { domain: Domain; initialBoardId?: string; onDrilldown: (value: Drilldown) => void }) {
  const [boards, setBoards] = useState<Dashboard[]>([]);
  const [selected, setSelected] = useState(initialBoardId || '');
  const [board, setBoard] = useState<Dashboard | null>(null);
  const [templates, setTemplates] = useState<DashboardTemplate[]>(domain.dashboard_templates || []);
  const [approvedDimensions, setApprovedDimensions] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [createOpen, setCreateOpen] = useState(false);
  const [newTitle, setNewTitle] = useState('');
  const [templateId, setTemplateId] = useState('');
  const [titleEdit, setTitleEdit] = useState(false);
  const [filters, setFilters] = useState<Record<string, string>>({});
  const [evidence, setEvidence] = useState<DashboardCard | null>(null);
  const [renameCard, setRenameCard] = useState<string | null>(null);
  const [cardTitle, setCardTitle] = useState('');
  const [drillCard, setDrillCard] = useState<DashboardCard | null>(null);
  const [drillDimension, setDrillDimension] = useState('');
  const [drillValue, setDrillValue] = useState('');
  const currentId = useRef(selected); currentId.current = selected;
  const loadList = useCallback(async () => {
    const result = orderedBoards(await api<Dashboard[]>(`/dashboards?domain_id=${encode(domain.id)}`), domain.model_version);
    setBoards(result);
    return result;
  }, [domain.id, domain.model_version]);
  const adopt = useCallback((value: Dashboard) => {
    setBoard(value);
    setFilters(Object.fromEntries(Object.entries(value.filters || {}).map(([key, entry]) => [key, typeof entry === 'string' ? entry : JSON.stringify(entry)])));
    setBoards(current => orderedBoards(current.some(item => item.id === value.id) ? current.map(item => item.id === value.id ? value : item) : [...current, value], domain.model_version));
  }, [domain.model_version]);
  useEffect(() => {
    let alive = true;
    setLoading(true); setError('');
    loadList().then(result => { if (alive && !currentId.current && result.length) setSelected(result[0].id); }).catch(err => { if (alive) setError(errorText(err)); }).finally(() => { if (alive) setLoading(false); });
    api<Domain>(`/domains/${encode(domain.id)}`).then(value => { if (alive) { setTemplates(value.dashboard_templates || []); setApprovedDimensions((value.dimensions || []).map(item => String(item.id))); } }).catch(() => { /* Board list remains usable if optional template metadata is unavailable. */ });
    return () => { alive = false; };
  }, [domain.id, loadList]);
  useEffect(() => { if (initialBoardId) setSelected(initialBoardId); }, [initialBoardId]);
  useEffect(() => {
    if (!selected) { setBoard(null); return; }
    let alive = true;
    setLoading(true); setError(''); setNotice('');
    api<Dashboard>(`/dashboards/${encode(selected)}`).then(value => { if (alive) adopt(value); }).catch(err => { if (alive) { setError(errorText(err)); setBoard(null); } }).finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [selected, adopt]);
  async function create(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError('');
    try { const value = await api<Dashboard>('/dashboards', { method: 'POST', body: JSON.stringify({ title: newTitle.trim(), domain_id: domain.id, ...(templateId ? { template_id: templateId } : {}) }) }); setSelected(value.id); adopt(value); setCreateOpen(false); setNewTitle(''); setTemplateId(''); }
    catch (err) { setError(errorText(err)); } finally { setBusy(false); }
  }
  async function patch(fields: Partial<Dashboard>) {
    if (!board) return; const id = board.id; setBusy(true); setError('');
    try { const value = await api<Dashboard>(`/dashboards/${encode(id)}`, { method: 'PATCH', body: JSON.stringify(fields) }); if (currentId.current === id) { adopt(value); setTitleEdit(false); setRenameCard(null); } }
    catch (err) { if (currentId.current === id) setError(errorText(err)); } finally { setBusy(false); }
  }
  async function removeBoard() {
    if (!board || !window.confirm(`删除看板“${board.title}”？不会删除源数据、历史分析或其他看板。`)) return;
    setBusy(true); setError('');
    try { await api(`/dashboards/${encode(board.id)}`, { method: 'DELETE' }); setBoard(null); const remaining = await loadList(); setSelected(remaining[0]?.id || ''); }
    catch (err) { setError(errorText(err)); } finally { setBusy(false); }
  }
  async function refresh(event?: FormEvent) {
    event?.preventDefault(); if (!board) return;
    const id = board.id; setBusy(true); setError(''); setNotice('');
    try {
      const value = await api<Dashboard>(`/dashboards/${encode(id)}/refresh`, { method: 'POST', body: JSON.stringify({ filters: parseFilters(filters) }) });
      if (currentId.current === id) { adopt(value); const failed = value.cards.filter(card => card.snapshot?.error || card.snapshot?.stale).length; setNotice(failed ? `刷新已结束，${failed} 张卡片失败或保留旧结果，请查看各卡片状态。` : '看板已刷新。此次仅执行已保存查询，没有调用分析模型。'); }
    } catch (err) { if (currentId.current === id) setError(errorText(err)); } finally { setBusy(false); }
  }
  function changeCard(id: string, changes: Partial<DashboardCard>) { if (board) void patch({ cards: board.cards.map(card => card.id === id ? { ...card, ...changes } : card) }); }
  function moveCard(id: string, direction: number) {
    if (!board) return; const cards = [...board.cards].sort((a, b) => a.layout.order - b.layout.order); const index = cards.findIndex(card => card.id === id); const next = index + direction;
    if (next < 0 || next >= cards.length) return;
    [cards[index], cards[next]] = [cards[next], cards[index]];
    void patch({ cards: cards.map((card, order) => ({ ...card, layout: { ...card.layout, order } })) });
  }
  async function drill(event: FormEvent) {
    event.preventDefault(); if (!board || !drillCard) return;
    setBusy(true); setError('');
    try { const value = await api<Drilldown>(`/dashboards/${encode(board.id)}/cards/${encode(drillCard.id)}/drilldown`, { method: 'POST', body: JSON.stringify({ dimension: drillDimension, value: drillValue }) }); setDrillCard(null); onDrilldown(value); }
    catch (err) { setError(errorText(err)); } finally { setBusy(false); }
  }
  const drillDimensions = (card: DashboardCard) => Object.keys(card.query.filter_bindings || {}).filter(key => approvedDimensions.includes(key));
  const bindings = [...new Set((board?.cards || []).flatMap(card => Object.keys(card.query.filter_bindings || {})))].filter(name => name !== 'date_from' && name !== 'date_to');
  const drillColumn = drillCard?.query.filter_bindings?.[drillDimension] || drillDimension;
  const drillQuery = drillCard ? [drillCard.snapshot?.query, ...(drillCard.snapshot?.queries || [])].find(query => query?.columns.includes(drillColumn)) : undefined;
  const drillValues = drillQuery ? [...new Set(drillQuery.rows.map(row => printable(cell(drillQuery, row, drillColumn))))].slice(0, 50) : [];
  const cards = [...(board?.cards || [])].sort((a, b) => a.layout.order - b.layout.order);
  const renderedFilters = (() => { try { return comparableFilters(parseFilters(filters)); } catch { return 'invalid'; } })();

  return <div className="enterprise-page dashboard-page">
    <div className="page-heading"><div><p className="eyebrow">BUSINESS DASHBOARDS</p><h1>把洞察，留在日常决策里。</h1><p>保存指标与查询，筛选、刷新，再从异常继续深入。</p></div><button className="primary-button" onClick={() => { setCreateOpen(true); setNewTitle(`${domain.title}看板`); }}><Icon name="plus" size={16} />新建看板</button></div>
    {error && <div className="error-banner" role="alert"><Icon name="info" size={17} /><span>{error}</span></div>}
    {notice && <div className="mode-notice live-notice"><Icon name="check" size={16} />{notice}</div>}
    <div className="board-switcher">{boards.map(item => <button key={item.id} className={selected === item.id ? 'active' : ''} onClick={() => { setSelected(item.id); setTitleEdit(false); }}><Icon name="grid" size={16} /><span>{item.title}</span><small>{item.cards?.length || 0} 张卡片</small></button>)}</div>
    {loading && <div className="loading-state"><span className="spinner" />加载看板…</div>}
    {!loading && !board && <div className="enterprise-empty"><Icon name="grid" size={38} /><h2>一张真正属于业务的看板</h2><p>从当前业务域的模板开始，或将已验证的分析结果固定到空白看板。没有查询结果时，我们不会填充模拟图表。</p><button className="primary-button" onClick={() => { setCreateOpen(true); setNewTitle(`${domain.title}看板`); }}>创建第一张看板<Icon name="arrow" size={15} /></button></div>}
    {!loading && board && <>
      {String(board.model_version) !== String(domain.model_version) && <div className="mode-notice warning-notice"><Icon name="info" size={16} />此看板保留旧语义模型 v{board.model_version} 的快照；当前业务域已更新为 v{domain.model_version}。刷新可能被版本校验拒绝，可基于当前版本新建看板。</div>}
      <div className="board-heading"><div>{titleEdit ? <form className="inline-edit" onSubmit={event => { event.preventDefault(); void patch({ title: newTitle.trim() }); }}><input aria-label="看板名称" value={newTitle} onChange={event => setNewTitle(event.target.value)} required autoFocus /><button type="submit" disabled={busy} className="primary-button">保存</button><button type="button" className="text-button" onClick={() => setTitleEdit(false)}>取消</button></form> : <h2>{board.title}<button className="icon-button" aria-label="重命名看板" onClick={() => { setNewTitle(board.title); setTitleEdit(true); }}><Icon name="edit" size={15} /></button></h2>}<p>语义模型 v{board.model_version || '—'}<span>·</span>上次刷新 {timeLabel(board.last_refresh_at)}</p></div><div className="toolbar-actions"><button className="secondary-button" disabled={busy} onClick={() => void refresh()}><Icon name="refresh" size={15} />{busy ? '处理中…' : '刷新数据'}</button><button className="icon-button danger" disabled={busy} aria-label="删除看板" onClick={() => void removeBoard()}><Icon name="trash" size={16} /></button></div></div>
      <form className="dashboard-filters" onSubmit={refresh}><div className="filter-caption"><Icon name="layers" size={17} /><strong>统一筛选</strong></div><label>开始日期<input type="date" value={filters.date_from || ''} onChange={event => setFilters({ ...filters, date_from: event.target.value })} /></label><label>结束日期<input type="date" value={filters.date_to || ''} onChange={event => setFilters({ ...filters, date_to: event.target.value })} /></label>{bindings.map(name => <label key={name}>{name}<input value={filters[name] || ''} placeholder="全部" onChange={event => setFilters({ ...filters, [name]: event.target.value })} /></label>)}<button type="submit" className="primary-button" disabled={busy}>应用并刷新</button><button type="button" className="text-button" onClick={() => setFilters({})}>清空筛选</button></form>
      <p className="filter-hint">筛选由服务端按字段绑定应用；不适用的条件会明确报错。月度卡片按完整月份筛选，不能据此推断任意日区间。查询时间不等于业务数据更新时间。</p>
      {!cards.length && <div className="enterprise-empty compact"><Icon name="chart" size={32} /><h3>看板已经准备好，等待你的第一条洞察</h3><p>在分析工作台完成真实分析后，使用“固定到看板”保存结果。</p></div>}
      <div className="dashboard-grid">{cards.map((card, index) => {
        const snapshot = card.snapshot; const query = snapshot?.query;
        const stale = !!snapshot?.stale || !!snapshot && comparableFilters(snapshot.filters || {}) !== renderedFilters;
        const chart: Chart = { ...card.chart, ...snapshot?.chart, title: card.title, query_id: query?.id || card.id, y: snapshot?.chart?.y || card.chart.y || [] };
        return <article key={card.id} className={`dashboard-widget ${card.layout.width === 12 ? 'full-width' : ''} ${stale ? 'stale' : ''}`}>
          <header><div><h3>{card.title}</h3><span className="widget-updated">{snapshot?.queried_at ? `查询于 ${timeLabel(snapshot.queried_at)}` : '尚未查询'} · 数据更新时间：{snapshot?.data_updated_at ? timeLabel(snapshot.data_updated_at) : '未知'}{snapshot?.analysis_generated_at ? ` · 分析生成于 ${timeLabel(snapshot.analysis_generated_at)}` : ''}</span></div><details className="widget-menu"><summary aria-label={`${card.title}卡片操作`}>···</summary><div><button onClick={() => { setRenameCard(card.id); setCardTitle(card.title); }}>重命名</button><button disabled={busy} onClick={() => changeCard(card.id, { layout: { ...card.layout, width: card.layout.width === 12 ? 6 : 12 } })}>{card.layout.width === 12 ? '半行宽度' : '整行宽度'}</button><button disabled={busy || index === 0} onClick={() => moveCard(card.id, -1)}>向前移动</button><button disabled={busy || index === cards.length - 1} onClick={() => moveCard(card.id, 1)}>向后移动</button><button className="danger" disabled={busy} onClick={() => { if (window.confirm(`从看板删除“${card.title}”？源数据不受影响。`)) void patch({ cards: board.cards.filter(item => item.id !== card.id) }); }}>删除卡片</button></div></details></header>
          {(snapshot?.error || stale) && <div className="widget-warning"><Icon name="info" size={14} /><span>{snapshot?.error || '筛选与当前快照不一致，请刷新。'}{query && ' 以下保留上次结果，不能视为本次刷新成功。'}</span></div>}
          {query ? <ChartView chart={chart} query={query} /> : <div className="widget-empty"><Icon name="chart" size={28} /><p>{snapshot?.error ? '本次查询未返回可展示的数据。' : '等待首次刷新；没有预填业务数值。'}</p></div>}
          <footer><span className="widget-recipe">{card.analysis_recipe?.tool || '受控 SQL 查询'}{card.query.metric_ids?.length ? ` · ${card.query.metric_ids.length} 项指标` : ''}</span><button className="text-button" onClick={() => setEvidence(card)}><Icon name="code" size={13} />证据</button><button className="text-button" disabled={!query || !drillDimensions(card).length} onClick={() => { setDrillCard(card); setDrillDimension(drillDimensions(card)[0] || ''); setDrillValue(''); }}><Icon name="arrow" size={13} />深入分析</button></footer>
        </article>;
      })}</div>
    </>}
    {createOpen && <div className="modal-backdrop" onClick={() => !busy && setCreateOpen(false)}><form className="enterprise-modal" onClick={event => event.stopPropagation()} onSubmit={create}><div className="modal-heading"><h2>创建企业看板</h2><button type="button" className="icon-button" aria-label="关闭新建看板" onClick={() => setCreateOpen(false)}><Icon name="close" /></button></div><p>看板始终绑定当前业务域及语义模型，不能跨权限范围混用数据。</p>{error && <div className="error-banner" role="alert">{error}</div>}<label>看板名称<input required maxLength={120} value={newTitle} onChange={event => setNewTitle(event.target.value)} /></label><label>从哪里开始<select value={templateId} onChange={event => setTemplateId(event.target.value)}><option value="">空白看板 · 从分析结果逐步积累</option>{templates.map(template => <option key={template.id} value={template.id}>{template.title}</option>)}</select></label><div className="modal-footer"><span>创建不会调用模型；数据在点击刷新后查询。</span><button type="submit" className="primary-button" disabled={busy || !newTitle.trim()}>创建看板</button></div></form></div>}
    {renameCard && <div className="modal-backdrop" onClick={() => setRenameCard(null)}><form className="enterprise-modal small" onClick={event => event.stopPropagation()} onSubmit={event => { event.preventDefault(); changeCard(renameCard, { title: cardTitle.trim() }); }}><div className="modal-heading"><h2>重命名卡片</h2><button type="button" className="icon-button" aria-label="关闭卡片编辑" onClick={() => setRenameCard(null)}><Icon name="close" /></button></div>{error && <div className="error-banner" role="alert">{error}</div>}<label>卡片标题<input required value={cardTitle} onChange={event => setCardTitle(event.target.value)} /></label><button className="primary-button" disabled={busy || !cardTitle.trim()}>保存</button></form></div>}
    {evidence && <div className="modal-backdrop" onClick={() => setEvidence(null)}><section className="enterprise-modal wide" onClick={event => event.stopPropagation()}><div className="modal-heading"><h2>{evidence.title} · 证据</h2><button className="icon-button" aria-label="关闭卡片证据" onClick={() => setEvidence(null)}><Icon name="close" /></button></div><p>指标：{evidence.query.metric_ids?.join('、') || '未指定'} · 语义模型 v{board?.model_version || '—'}</p>{evidence.provenance && <p>来源任务 {evidence.provenance.run_id} / {evidence.provenance.query_id}</p>}<CalculationPanels calculations={evidence.snapshot?.calculation ? [evidence.snapshot.calculation] : []} /><pre className="sql-code">{evidence.snapshot?.query?.sql || evidence.query.sql}</pre>{evidence.snapshot?.queries?.map(query => <details className="discovery-details" key={query.id}><summary>原始查询证据 · {query.id}</summary><pre className="sql-code">{query.sql}</pre><DataTable query={query} /></details>)}<details className="discovery-details"><summary>筛选绑定与分析方法</summary><pre>{JSON.stringify({ filter_bindings: evidence.query.filter_bindings, snapshot_filters: evidence.snapshot?.filters, analysis_recipe: evidence.analysis_recipe }, null, 2)}</pre></details>{evidence.snapshot?.query ? <DataTable query={evidence.snapshot.query} /> : <div className="empty-state compact">尚无查询快照。</div>}</section></div>}
    {drillCard && <div className="modal-backdrop" onClick={() => setDrillCard(null)}><form className="enterprise-modal" onClick={event => event.stopPropagation()} onSubmit={drill}><div className="modal-heading"><h2>从卡片继续分析</h2><button type="button" className="icon-button" aria-label="关闭下钻" onClick={() => setDrillCard(null)}><Icon name="close" /></button></div><p>带入卡片证据和当前筛选到分析工作台，由你确认问题后提交，不会自动调用模型。</p>{error && <div className="error-banner" role="alert">{error}</div>}<label>分析维度<select value={drillDimension} onChange={event => setDrillDimension(event.target.value)}><option value="">请选择维度</option>{drillDimensions(drillCard).map(value => <option key={value} value={value}>{value}</option>)}</select></label><label>关注的值<input required value={drillValue} onChange={event => setDrillValue(event.target.value)} placeholder="例如：某渠道、地区或月份" list="drill-values" /><datalist id="drill-values">{drillValues.map(value => <option key={value} value={value} />)}</datalist></label><button type="submit" className="primary-button" disabled={busy || !drillDimension || !drillValue.trim()}>打开分析工作台<Icon name="arrow" size={15} /></button></form></div>}
  </div>;
}

export function PinResult({ domain, run, queryId, chartIndex, onClose, onSaved }: { domain: Domain; run: Run; queryId: string; chartIndex?: number; onClose: () => void; onSaved: (id: string) => void }) {
  const [boards, setBoards] = useState<Dashboard[]>([]);
  const [selected, setSelected] = useState('');
  const [title, setTitle] = useState(`${domain.title}看板`);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  useEffect(() => { api<Dashboard[]>(`/dashboards?domain_id=${encode(domain.id)}`).then(result => { setBoards(result); setSelected(result[0]?.id || 'new'); }).catch(err => setError(errorText(err))); }, [domain.id]);
  async function save(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError('');
    try {
      let id = selected;
      if (id === 'new') {
        const created = await api<Dashboard>('/dashboards', { method: 'POST', body: JSON.stringify({ title: title.trim(), domain_id: domain.id }) });
        id = created.id; setBoards(current => [...current, created]); setSelected(id);
      }
      await api(`/dashboards/${encode(id)}/cards`, { method: 'POST', body: JSON.stringify({ run_id: run.run_id, query_id: queryId, ...(chartIndex !== undefined ? { chart_index: chartIndex } : {}) }) });
      onSaved(id);
    } catch (err) { setError(errorText(err)); } finally { setBusy(false); }
  }
  return <div className="modal-backdrop" onClick={() => !busy && onClose()}><form className="enterprise-modal" onClick={event => event.stopPropagation()} onSubmit={save}><div className="modal-heading"><h2>固定分析结果到看板</h2><button type="button" className="icon-button" aria-label="关闭固定结果" onClick={onClose}><Icon name="close" /></button></div><p>保存经过校验的查询、图表及来源，后续可重新刷新数据。</p>{error && <div className="error-banner" role="alert">{error}</div>}<label>目标看板<select required value={selected} onChange={event => setSelected(event.target.value)}><option value="" disabled>请选择看板</option>{boards.map(item => <option key={item.id} value={item.id}>{item.title}</option>)}<option value="new">创建新看板</option></select></label>{selected === 'new' && <label>新看板名称<input required value={title} onChange={event => setTitle(event.target.value)} /></label>}<button className="primary-button" disabled={busy || !selected || selected === 'new' && !title.trim()} type="submit">保存并查看看板<Icon name="arrow" size={15} /></button></form></div>;
}
