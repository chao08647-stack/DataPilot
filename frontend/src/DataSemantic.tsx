import { useCallback, useEffect, useState } from 'react';
import type { FormEvent } from 'react';
import { api, errorText, printable } from './api';
import type { DataSource, Domain, RecordValue, SemanticModel, TableMeta } from './api';
import { Icon } from './AnalysisWorkspace';
import { domainDisplayDescription, sourceDisplayName } from './displayLabels';

const encode = encodeURIComponent;
function columns(table: TableMeta): { name: string; type: string }[] {
  return Array.isArray(table.columns) ? table.columns.map(column => ({ name: column.name, type: column.type || column.data_type || '' })) : Object.entries(table.columns || {}).map(([name, type]) => ({ name, type: printable(type) }));
}
function emptyDefinition(domain: Domain, tables: TableMeta[] = []): RecordValue {
  return { id: domain.id, title: domain.title, description: domain.description, tables: tables.map(table => ({ name: table.name, description: table.description || '', grain: table.grain || '', columns: Object.fromEntries(columns(table).map(column => [column.name, column.type])) })), relations: [], metrics: [], dimensions: [] };
}

export default function DataSemantic({ domain, domains, onDomainCreated, onChanged }: { domain?: Domain; domains: Domain[]; onDomainCreated: (domain: Domain) => void; onChanged: () => void }) {
  const [tab, setTab] = useState<'sources' | 'domains' | 'semantic'>('sources');
  const [sources, setSources] = useState<DataSource[]>([]);
  const [models, setModels] = useState<SemanticModel[]>([]);
  const [currentModel, setCurrentModel] = useState<SemanticModel | null>(null);
  const [definition, setDefinition] = useState(domain ? JSON.stringify(emptyDefinition(domain), null, 2) : '{}');
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [sourceOpen, setSourceOpen] = useState(false);
  const [domainOpen, setDomainOpen] = useState(false);
  const [sourceKind, setSourceKind] = useState<DataSource['kind']>('postgres');
  const [sourceName, setSourceName] = useState('');
  const [sourceId, setSourceId] = useState('');
  const [host, setHost] = useState('127.0.0.1');
  const [port, setPort] = useState('5432');
  const [database, setDatabase] = useState('');
  const [user, setUser] = useState('');
  const [passwordEnv, setPasswordEnv] = useState('');
  const [schemas, setSchemas] = useState('public');
  const [allowedTables, setAllowedTables] = useState('');
  const [filePath, setFilePath] = useState('');
  const [consentMetadata, setConsentMetadata] = useState(false);
  const [consentResults, setConsentResults] = useState(false);
  const [domainId, setDomainId] = useState('');
  const [domainTitle, setDomainTitle] = useState('');
  const [domainDescription, setDomainDescription] = useState('');
  const [domainSource, setDomainSource] = useState('');
  const [inspection, setInspection] = useState<{ source: DataSource; tables: TableMeta[] } | null>(null);
  const [selectedTables, setSelectedTables] = useState<string[]>([]);
  const [sourceStatus, setSourceStatus] = useState<Record<string, string>>({});
  const [consentSource, setConsentSource] = useState<DataSource | null>(null);
  const [sourceMetadataAccess, setSourceMetadataAccess] = useState(false);
  const [sourceResultsAccess, setSourceResultsAccess] = useState(false);
  const loadSources = useCallback(async () => { const result = await api<DataSource[]>('/data-sources'); setSources(result); return result; }, []);
  const loadModels = useCallback(async () => {
    if (!domain) { setModels([]); return []; }
    const result = await api<SemanticModel[]>(`/semantic-models?domain_id=${encode(domain.id)}`); setModels(result); return result;
  }, [domain?.id]);
  useEffect(() => {
    let alive = true; setLoading(true); setError('');
    Promise.all([loadSources(), loadModels()]).then(([, result]) => {
      if (!alive) return;
      const model = result.filter(item => item.status === 'draft').at(-1) || result.find(item => String(item.version) === String(domain?.model_version)) || result.at(-1);
      if (model) { setCurrentModel(model); setDefinition(JSON.stringify(model.definition, null, 2)); }
      else { setCurrentModel(null); setDefinition(domain ? JSON.stringify(emptyDefinition(domain), null, 2) : '{}'); }
    }).catch(err => { if (alive) setError(errorText(err)); }).finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [loadSources, loadModels, domain?.id]);
  function consentConfirmed(metadata: boolean, results: boolean) { return (!metadata && !results) || window.confirm('确认允许当前配置的外部模型服务接收所勾选的数据？表名、字段及查询结果可能包含企业信息。请先确认数据分类和组织授权。'); }
  async function createSource(event: FormEvent) {
    event.preventDefault(); if (!consentConfirmed(consentMetadata, consentResults)) return;
    setBusy(true); setError(''); setNotice('');
    const config: RecordValue = sourceKind === 'duckdb' ? { path: filePath.trim() } : { host: host.trim(), port: Number(port), database: database.trim(), user: user.trim(), password_env: passwordEnv.trim(), schemas: schemas.split(',').map(item => item.trim()).filter(Boolean), allowed_tables: allowedTables.split(',').map(item => item.trim()).filter(Boolean) };
    try { await api('/data-sources', { method: 'POST', body: JSON.stringify({ id: sourceId.trim(), name: sourceName.trim(), kind: sourceKind, config, model_access: { metadata: consentMetadata, results: consentResults } }) }); await loadSources(); onChanged(); setSourceOpen(false); setNotice('数据源已登记。请先测试连接、查看元数据，再建立业务域与语义模型。'); }
    catch (err) { setError(errorText(err)); } finally { setBusy(false); }
  }
  async function createDomain(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError('');
    try { const result = await api<Domain>('/domains', { method: 'POST', body: JSON.stringify({ id: domainId.trim(), title: domainTitle.trim(), description: domainDescription.trim(), data_source_id: domainSource }) }); setDomainOpen(false); onDomainCreated(result); setNotice('业务域已创建。发布经确认的语义模型后，才能开始分析。'); setTab('semantic'); }
    catch (err) { setError(errorText(err)); } finally { setBusy(false); }
  }
  async function testSource(source: DataSource) {
    setBusy(true); setError('');
    try { const result = await api<RecordValue>(`/data-sources/${encode(source.id)}/test`, { method: 'POST' }); setSourceStatus(current => ({ ...current, [source.id]: printable(result.message || result.status || (result.ok === true ? '连接正常' : result.ok === false ? '连接失败' : '检测已返回')) })); }
    catch (err) { setSourceStatus(current => ({ ...current, [source.id]: '连接检测失败' })); setError(errorText(err)); } finally { setBusy(false); }
  }
  async function inspect(source: DataSource) {
    setBusy(true); setError('');
    try { const result = await api<TableMeta[] | { tables: TableMeta[] }>(`/data-sources/${encode(source.id)}/introspect`, { method: 'POST' }); setInspection({ source, tables: Array.isArray(result) ? result : result.tables || [] }); setSelectedTables([]); }
    catch (err) { setError(errorText(err)); } finally { setBusy(false); }
  }
  async function updateConsent(event: FormEvent) {
    event.preventDefault(); if (!consentSource || !consentConfirmed(sourceMetadataAccess, sourceResultsAccess)) return;
    setBusy(true); setError('');
    try { await api(`/data-sources/${encode(consentSource.id)}`, { method: 'PATCH', body: JSON.stringify({ model_access: { metadata: sourceMetadataAccess, results: sourceResultsAccess } }) }); await loadSources(); setConsentSource(null); onChanged(); setNotice('模型访问授权已更新；服务端仍会检查数据源范围和语义模型。'); }
    catch (err) { setError(errorText(err)); } finally { setBusy(false); }
  }
  async function saveDraft() {
    if (!domain) return; setBusy(true); setError(''); setNotice('');
    try { const parsed: unknown = JSON.parse(definition); if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') throw new Error('语义模型必须是 JSON 对象。'); const result = await api<SemanticModel>('/semantic-models', { method: 'POST', body: JSON.stringify({ domain_id: domain.id, definition: parsed }) }); await loadModels(); setCurrentModel(result); setDefinition(JSON.stringify(result.definition, null, 2)); setNotice('语义草稿已保存，尚未对任务生效。请审核粒度、关联路径和指标公式后发布。'); }
    catch (err) { setError(errorText(err)); } finally { setBusy(false); }
  }
  async function publish() {
    if (!currentModel || !window.confirm('确认发布此语义模型？请核对允许表、关联键、业务粒度、指标公式与维度。运行中的任务可能阻止发布；历史任务保留原模型版本。')) return;
    setBusy(true); setError('');
    try { await api(`/semantic-models/${encode(currentModel.id)}/publish`, { method: 'POST' }); const result = await loadModels(); const published = result.find(model => model.id === currentModel.id); if (published) setCurrentModel(published); onChanged(); setNotice('语义模型已发布。新任务使用新版本，旧任务与历史证据保持可追溯。'); }
    catch (err) { setError(errorText(err)); } finally { setBusy(false); }
  }
  function useTables() {
    if (!domain || !inspection || inspection.source.id !== domain.data_source_id) return;
    setCurrentModel(null); setDefinition(JSON.stringify(emptyDefinition(domain, inspection.tables.filter(table => selectedTables.includes(table.name))), null, 2)); setInspection(null); setTab('semantic'); setNotice('已填入选中表的真实结构。请补全关联、指标与维度；当前只是未保存草稿。');
  }
  const dirty = currentModel ? definition !== JSON.stringify(currentModel.definition, null, 2) : true;

  return <div className="enterprise-page data-page"><div className="page-heading"><div><p className="eyebrow">DATA & SEMANTICS</p><h1>数据源与语义模型</h1><p>数据源决定能访问什么，语义模型决定如何正确地理解和关联。</p></div><button className="primary-button" onClick={() => { setSourceOpen(true); setSourceId(`source-${crypto.randomUUID().slice(0, 8)}`); setSourceName(''); setConsentMetadata(false); setConsentResults(false); }}><Icon name="plus" size={16} />接入数据源</button></div>
    <div className="enterprise-tabs" role="tablist">{[['sources', '数据源'], ['domains', '业务域'], ['semantic', '语义模型']].map(([key, title]) => <button role="tab" aria-selected={tab === key} key={key} className={tab === key ? 'active' : ''} onClick={() => setTab(key as typeof tab)}>{title}</button>)}</div>
    {error && <div className="error-banner" role="alert"><Icon name="info" size={17} /><span>{error}</span></div>}{notice && <div className="mode-notice live-notice"><Icon name="check" size={16} />{notice}</div>}
    {loading && <div className="loading-state"><span className="spinner" />读取资源配置…</div>}
    {tab === 'sources' && <><div className="security-note"><Icon name="info" size={19} /><p><strong>凭据留在服务端，授权保持显式。</strong>连接只保存密码环境变量的名称，不保存明文密码。外部数据库默认不允许向模型发送元数据或查询结果。</p></div><div className="source-grid">{sources.map(source => <article className="source-card" key={source.id}><header><span className={`source-logo ${source.kind}`}><Icon name="database" size={23} /></span><div><h3>{sourceDisplayName(source)}</h3><p>{source.kind.toUpperCase()}<span>·</span>{source.builtin ? '本地数据源' : '自定义数据源'}</p></div></header><div className="source-details"><span>来源标识</span><code>{source.id}</code><span>数据库 / 文件</span><code>{printable(source.config.database || source.config.path)}</code><span>模型访问</span><div className="consent-badges"><span className={source.model_access?.metadata ? 'granted' : ''}>元数据 {source.model_access?.metadata ? '已授权' : '未授权'}</span><span className={source.model_access?.results ? 'granted' : ''}>查询结果 {source.model_access?.results ? '已授权' : '未授权'}</span></div></div>{sourceStatus[source.id] && <p className="connection-result">{sourceStatus[source.id]}</p>}<footer><button disabled={busy} onClick={() => void testSource(source)} className="text-button"><Icon name="check" size={14} />测试连接</button><button disabled={busy} onClick={() => void inspect(source)} className="text-button"><Icon name="list" size={14} />查看表结构</button><button className="text-button" onClick={() => { setConsentSource(source); setSourceMetadataAccess(!!source.model_access?.metadata); setSourceResultsAccess(!!source.model_access?.results); }}>模型授权</button></footer></article>)}</div>{!loading && !sources.length && <div className="enterprise-empty"><Icon name="database" size={35} /><h2>接入你的第一个数据源</h2><p>支持 PostgreSQL 与本地 DuckDB。MySQL 接入暂不开放。建议使用专用只读账号和明确的表范围。</p></div>}</>}
    {tab === 'domains' && <><div className="section-heading"><h2>业务范围，不再受行业限制</h2><button className="secondary-button" disabled={!sources.length} onClick={() => { setDomainOpen(true); setDomainId(`domain-${crypto.randomUUID().slice(0, 8)}`); setDomainTitle(''); setDomainSource(sources[0]?.id || ''); }}><Icon name="plus" size={15} />建立业务域</button></div><div className="domain-grid">{domains.map(item => <article className={`domain-card ${item.id === domain?.id ? 'selected' : ''}`} key={item.id}><div className="card-eyebrow">{item.synthetic ? '业务域' : '自定义业务域'}<span>{item.model_version ? `语义模型 v${item.model_version}` : '待发布语义模型'}</span></div><h3>{item.title}</h3><p>{domainDisplayDescription(item)}</p><dl><dt>数据源</dt><dd>{sources.find(source => source.id === item.data_source_id) ? sourceDisplayName(sources.find(source => source.id === item.data_source_id)!) : item.data_source_id}</dd><dt>正式指标</dt><dd>{item.metrics?.length || 0}</dd></dl><button className="text-button" onClick={() => { onDomainCreated(item); setTab('semantic'); }}>查看语义模型<Icon name="arrow" size={14} /></button></article>)}</div></>}
    {tab === 'semantic' && (domain ? <><div className="semantic-toolbar"><div><h2>{domain.title}</h2><p>指标公式、允许的关联关系与统计粒度，需要人工确认后发布。</p></div><select aria-label="语义模型版本" value={currentModel?.id || ''} onChange={event => { const model = models.find(item => item.id === event.target.value); if (model) { setCurrentModel(model); setDefinition(JSON.stringify(model.definition, null, 2)); } }}><option value="">未保存草稿</option>{[...models].reverse().map(model => <option key={model.id} value={model.id}>v{model.version} · {model.status === 'published' ? '已发布' : '草稿'}</option>)}</select></div><div className="semantic-layout"><div className="semantic-editor"><div className="code-toolbar"><span>definition.json</span><span>{dirty ? '未保存修改' : currentModel?.status === 'published' ? '已发布 · 修改后另存草稿' : '已保存草稿'}</span></div><textarea aria-label="语义模型 JSON" spellCheck={false} value={definition} onChange={event => setDefinition(event.target.value)} /><div className="semantic-actions"><button className="secondary-button" disabled={busy} onClick={() => { const source = sources.find(item => item.id === domain.data_source_id); if (source) void inspect(source); }}><Icon name="database" size={15} />从真实元数据选表</button><div className="toolbar-spacer" /><button className="secondary-button" disabled={busy} onClick={() => void saveDraft()}>保存为新草稿</button><button className="primary-button" disabled={busy || !currentModel || currentModel.status !== 'draft' || dirty} onClick={() => void publish()}>确认并发布</button></div></div><aside className="semantic-guide"><Icon name="book" size={23} /><h3>发布前，检查这四件事</h3><ol><li><strong>表与字段</strong><span>只保留当前业务域允许分析的真实表。</span></li><li><strong>业务粒度</strong><span>明确“一行代表什么”，避免跨表重复计数。</span></li><li><strong>关联关系</strong><span>关联键、复合键与方向应来自可验证关系。</span></li><li><strong>指标与维度</strong><span>补全公式、状态过滤、时间口径和可用维度。</span></li></ol><p>草稿不会影响现有任务。发布被运行中任务阻止时，请先等待任务结束。</p></aside></div></> : <div className="enterprise-empty"><Icon name="book" size={32} /><h3>先建立业务域，再定义语义</h3><p>请从数据源中划定业务范围，之后管理其指标和关联关系。</p></div>)}
    {sourceOpen && <div className="modal-backdrop" onClick={() => !busy && setSourceOpen(false)}><form className="enterprise-modal wide" onClick={event => event.stopPropagation()} onSubmit={createSource}><div className="modal-heading"><h2>接入数据源</h2><button type="button" className="icon-button" aria-label="关闭数据源配置" onClick={() => setSourceOpen(false)}><Icon name="close" /></button></div>{error && <div className="error-banner" role="alert">{error}</div>}<div className="form-grid"><label>名称<input required value={sourceName} onChange={event => setSourceName(event.target.value)} placeholder="例如：经营分析只读库" /></label><label>资源标识<input required pattern="[A-Za-z0-9_-]+" value={sourceId} onChange={event => setSourceId(event.target.value)} /></label><label>连接类型<select value={sourceKind} onChange={event => { const kind = event.target.value as DataSource['kind']; setSourceKind(kind); setPort('5432'); setSchemas(kind === 'postgres' ? 'public' : ''); }}><option value="postgres">PostgreSQL</option><option value="duckdb">DuckDB 本地文件</option></select></label>{sourceKind === 'duckdb' ? <label>服务端数据文件路径<input required value={filePath} onChange={event => setFilePath(event.target.value)} placeholder="data/my-analytics.duckdb" /></label> : <><label>主机<input required value={host} onChange={event => setHost(event.target.value)} /></label><label>端口<input required type="number" min={1} max={65535} value={port} onChange={event => setPort(event.target.value)} /></label><label>数据库<input required value={database} onChange={event => setDatabase(event.target.value)} /></label><label>只读用户名<input required value={user} onChange={event => setUser(event.target.value)} /></label><label>密码环境变量名称<input required pattern="[A-Za-z_][A-Za-z0-9_]*" value={passwordEnv} onChange={event => setPasswordEnv(event.target.value)} placeholder="INSIGHT_ANALYTICS_PASSWORD" autoComplete="off" /></label><label>允许的 Schema（逗号分隔）<input value={schemas} onChange={event => setSchemas(event.target.value)} /></label><label>允许的表（逗号分隔）<input value={allowedTables} onChange={event => setAllowedTables(event.target.value)} placeholder="orders,order_items,customers" /></label></>}</div><div className="consent-form"><h3>向模型提供数据 · 默认关闭</h3><label><input type="checkbox" checked={consentMetadata} onChange={event => setConsentMetadata(event.target.checked)} />允许发送表名、字段、指标等元数据</label><label><input type="checkbox" checked={consentResults} onChange={event => setConsentResults(event.target.checked)} />允许发送受控查询返回的业务结果</label><p>请先确认企业数据授权。数据库密码不会发送给模型或保存到前端。</p></div><div className="modal-footer"><span>连接范围变更请新建数据源，避免影响历史任务。</span><button className="primary-button" disabled={busy} type="submit">保存数据源</button></div></form></div>}
    {domainOpen && <div className="modal-backdrop" onClick={() => !busy && setDomainOpen(false)}><form className="enterprise-modal" onClick={event => event.stopPropagation()} onSubmit={createDomain}><div className="modal-heading"><h2>建立业务域</h2><button className="icon-button" type="button" aria-label="关闭业务域配置" onClick={() => setDomainOpen(false)}><Icon name="close" /></button></div>{error && <div className="error-banner" role="alert">{error}</div>}<label>业务域名称<input required value={domainTitle} onChange={event => setDomainTitle(event.target.value)} placeholder="例如：供应链与履约" /></label><label>资源标识<input required pattern="[A-Za-z0-9_-]+" value={domainId} onChange={event => setDomainId(event.target.value)} /></label><label>绑定数据源<select required value={domainSource} onChange={event => setDomainSource(event.target.value)}>{sources.map(source => <option key={source.id} value={source.id}>{sourceDisplayName(source)} · {source.kind}</option>)}</select></label><label>业务范围说明<textarea rows={3} value={domainDescription} onChange={event => setDomainDescription(event.target.value)} placeholder="说明业务对象、分析目标和范围边界" /></label><button className="primary-button" disabled={busy} type="submit">建立业务域</button></form></div>}
    {inspection && <div className="modal-backdrop" onClick={() => setInspection(null)}><section className="enterprise-modal wide" onClick={event => event.stopPropagation()}><div className="modal-heading"><div><h2>{sourceDisplayName(inspection.source)} · 表结构</h2><p>实际元数据读取结果，不调用模型、不展示业务行。</p></div><button className="icon-button" aria-label="关闭元数据" onClick={() => setInspection(null)}><Icon name="close" /></button></div><div className="metadata-tables">{inspection.tables.map(table => <details className="metadata-table" key={table.name}><summary><label onClick={event => event.stopPropagation()}><input type="checkbox" checked={selectedTables.includes(table.name)} onChange={event => setSelectedTables(current => event.target.checked ? [...current, table.name] : current.filter(name => name !== table.name))} />{table.name}</label><span>{columns(table).length} 个字段</span></summary><div className="table-scroll"><table><thead><tr><th>字段</th><th>类型</th></tr></thead><tbody>{columns(table).map(column => <tr key={column.name}><td>{column.name}</td><td>{column.type}</td></tr>)}</tbody></table></div></details>)}{!inspection.tables.length && <div className="empty-state">没有返回可访问的表，请检查连接范围与只读账号权限。</div>}</div><div className="modal-footer"><span>{inspection.source.id !== domain?.data_source_id ? '请先切换或建立绑定此数据源的业务域。' : `已选 ${selectedTables.length} 张表；关联与指标仍需人工补充。`}</span><button className="primary-button" disabled={!selectedTables.length || inspection.source.id !== domain?.data_source_id} onClick={useTables}>用于当前业务域草稿</button></div></section></div>}
    {consentSource && <div className="modal-backdrop" onClick={() => setConsentSource(null)}><form className="enterprise-modal" onClick={event => event.stopPropagation()} onSubmit={updateConsent}><div className="modal-heading"><h2>{sourceDisplayName(consentSource)} · 模型授权</h2><button type="button" className="icon-button" aria-label="关闭模型授权" onClick={() => setConsentSource(null)}><Icon name="close" /></button></div>{error && <div className="error-banner" role="alert">{error}</div>}<div className="consent-form"><label><input type="checkbox" checked={sourceMetadataAccess} onChange={event => setSourceMetadataAccess(event.target.checked)} />允许模型读取元数据</label><label><input type="checkbox" checked={sourceResultsAccess} onChange={event => setSourceResultsAccess(event.target.checked)} />允许模型读取查询结果</label><p>未授权时真实分析将被服务端拒绝。手动查看元数据不等于已授权发送给外部模型。</p></div><button className="primary-button" disabled={busy} type="submit">确认并保存授权</button></form></div>}
  </div>;
}
