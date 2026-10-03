import { useCallback, useEffect, useState } from 'react';
import { api, errorText } from './api';
import type { DataSource, Domain, Health, Run, RunSummary } from './api';
import AnalysisWorkspace, { Icon, ResourceDrawer } from './AnalysisWorkspace';
import DataSemantic from './DataSemantic';
import RecentRuns from './RecentRuns';
import { BRAND_NAME, BrandIcon, BrandWordmark } from './Brand';

type Section = 'analysis' | 'data';
// Old dashboard/overview links return to analysis; stored resources stay intact.
function currentSection(): Section { return window.location.hash === '#data' ? 'data' : 'analysis'; }
function savedDomain() { try { return localStorage.getItem('insight-agents.enterprise.domain') || ''; } catch { return ''; } }

export default function App() {
  const [section, setSection] = useState<Section>(currentSection);
  const [domains, setDomains] = useState<Domain[]>([]);
  const [domainId, setDomainId] = useState(savedDomain);
  const [health, setHealth] = useState<Health | null>(null);
  const [sources, setSources] = useState<DataSource[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [incomingRunId, setIncomingRunId] = useState<string>();
  const [activeRunId, setActiveRunId] = useState<string>();
  const [historyVersion, setHistoryVersion] = useState(0);
  const [resetKey, setResetKey] = useState(0);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [resource, setResource] = useState<'memories' | 'skills' | null>(null);
  const domain = domains.find(item => item.id === domainId);
  const source = sources.find(item => item.id === domain?.data_source_id);
  const navigate = useCallback((next: Section) => { setSection(next); setSidebarOpen(false); window.history.replaceState(null, '', '#' + next); }, []);
  const reload = useCallback(async () => {
    setError('');
    const results = await Promise.allSettled([api<Domain[]>('/domains'), api<Health>('/health'), api<DataSource[]>('/data-sources')]);
    if (results[0].status === 'fulfilled') {
      const available = results[0].value; setDomains(available);
      setDomainId(current => available.some(item => item.id === current) ? current : available[0]?.id || '');
    } else setError(errorText(results[0].reason));
    setHealth(results[1].status === 'fulfilled' ? results[1].value : null);
    setSources(results[2].status === 'fulfilled' ? results[2].value : []);
    setLoading(false);
  }, []);
  useEffect(() => { void reload(); }, [reload]);
  useEffect(() => {
    const changed = () => setSection(currentSection());
    window.addEventListener('hashchange', changed);
    return () => window.removeEventListener('hashchange', changed);
  }, []);
  useEffect(() => { try { localStorage.setItem('insight-agents.enterprise.domain', domainId); } catch { /* Optional convenience. */ } }, [domainId]);
  useEffect(() => {
    if (!sidebarOpen) return;
    const close = (event: KeyboardEvent) => { if (event.key === 'Escape') setSidebarOpen(false); };
    window.addEventListener('keydown', close);
    return () => window.removeEventListener('keydown', close);
  }, [sidebarOpen]);
  const runConsumed = useCallback(() => setIncomingRunId(undefined), []);
  const runChanged = useCallback((run: Run | null) => { setActiveRunId(run?.run_id); setHistoryVersion(version => version + 1); }, []);
  const closeResource = useCallback(() => setResource(null), []);
  function selectDomain(id: string) {
    if (id === domainId) return;
    setDomainId(id); setIncomingRunId(undefined); setActiveRunId(undefined); setResource(null);
    try { localStorage.removeItem('insight-agents.workspace.v1'); } catch { /* Never carry threads across domains. */ }
  }
  function domainCreated(value: Domain) {
    setDomains(current => current.some(item => item.id === value.id) ? current.map(item => item.id === value.id ? value : item) : [...current, value]);
    selectDomain(value.id); void reload();
  }
  function openHistory(value: RunSummary) {
    const id = value.domain_id || value.scenario_id;
    if (!id || !domains.some(item => item.id === id)) { setError('该分析所属业务域不可用，无法打开记录。'); return; }
    if (id !== domainId) selectDomain(id);
    setIncomingRunId(value.run_id); setActiveRunId(value.run_id); navigate('analysis');
  }
  function newAnalysis() {
    setIncomingRunId(undefined); setActiveRunId(undefined); setResetKey(key => key + 1);
    try { localStorage.removeItem('insight-agents.workspace.v1'); } catch { /* Optional state. */ }
    navigate('analysis');
  }

  return <div className={'enterprise-shell light-bi chat-shell' + (sidebarOpen ? ' sidebar-open' : '')}>
    <header className="chat-topbar">
      <button className="icon-button mobile-history-toggle" aria-label={sidebarOpen ? '收起历史对话' : '展开历史对话'} aria-expanded={sidebarOpen} aria-controls="chat-sidebar" onClick={() => setSidebarOpen(value => !value)}><Icon name={sidebarOpen ? 'close' : 'panel'} /></button>
      <a className="chat-brand" href="#analysis" onClick={() => navigate('analysis')} aria-label={`${BRAND_NAME} 分析工作台`}><span className="chat-logo"><BrandIcon /></span><BrandWordmark /></a>
      <div className="domain-picker"><Icon name="database" size={16} /><select aria-label="当前业务域" value={domainId} onChange={event => selectDomain(event.target.value)}>{!domains.length && <option value="">选择业务数据</option>}{domains.map(item => <option key={item.id} value={item.id}>{item.title}</option>)}</select></div>
      <nav className="advanced-nav" aria-label="高阶功能">
        {section === 'data' && <button onClick={() => navigate('analysis')}><Icon name="arrow" size={16} /><span>返回分析</span></button>}
        <button className={section === 'data' ? 'active' : ''} aria-label="数据与语义" title="数据与语义" onClick={() => navigate('data')}><Icon name="database" size={17} /><span>数据与语义</span></button>
        <button aria-label="分析方法" title="分析方法" disabled={!domain} onClick={() => setResource('skills')}><Icon name="layers" size={17} /><span>分析方法</span></button>
        <button aria-label="知识与偏好" title="知识与偏好" disabled={!domain} onClick={() => setResource('memories')}><Icon name="book" size={17} /><span>知识与偏好</span></button>
      </nav>
    </header>
    <button className="sidebar-scrim" aria-label="关闭历史导航" tabIndex={sidebarOpen ? 0 : -1} onClick={() => setSidebarOpen(false)} />
    <aside className="enterprise-sidebar" id="chat-sidebar" aria-label="对话导航">
      <button className="new-analysis-button" onClick={newAnalysis}><Icon name="plus" size={19} /><span>新建分析</span></button>
      <RecentRuns domains={domains} selectedRunId={activeRunId} version={historyVersion} onOpen={openHistory} />
      <div className="sidebar-native"><span className={'status-dot ' + (health?.postgres_ready ? 'online' : '')} /><span>本地工作区</span><button className="icon-button" aria-label="刷新工作区状态" title="刷新连接状态，不重新分析" onClick={() => void reload()}><Icon name="refresh" size={15} /></button></div>
    </aside>
    <main className="enterprise-main">
      {error && <div className="error-banner global-error" role="alert"><Icon name="info" size={18} /><span>{error}</span><button className="text-button" onClick={() => void reload()}>重试</button></div>}
      {loading && <div className="loading-state"><span className="spinner" />加载工作区…</div>}
      {/* Keep the conversation mounted while configuration is open: no lost draft or resubmission. */}
      {!loading && <div className="enterprise-content analysis-content" hidden={section !== 'analysis'}>
        {domain ? <AnalysisWorkspace key={domain.id} domain={domain} externalRunId={incomingRunId} resetKey={resetKey} onRunConsumed={runConsumed} onRunChange={runChanged} /> : <div className="enterprise-empty"><Icon name="database" size={38} /><h2>先连接你的业务数据</h2><p>选择数据表并发布指标口径后，就可以用自然语言开始分析。</p><button className="primary-button" onClick={() => navigate('data')}>前往数据与语义<Icon name="arrow" size={16} /></button></div>}
      </div>}
      {!loading && section === 'data' && <div className="enterprise-content"><DataSemantic domain={domain} domains={domains} onDomainCreated={domainCreated} onChanged={() => void reload()} /></div>}
    </main>
    {resource && domain && <ResourceDrawer scenarioId={domain.id} scenarioTitle={domain.title} section={resource} onSection={setResource} onClose={closeResource} />}
    {source && !source.builtin && (!source.model_access?.metadata || !source.model_access?.results) && section === 'analysis' && <div className="consent-toast"><Icon name="info" size={16} /><span>数据源尚未完整授权模型访问，服务端会拒绝未授权分析。</span><button onClick={() => navigate('data')}>核对授权</button></div>}
  </div>;
}
