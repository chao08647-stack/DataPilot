import { useEffect, useState } from 'react';
import { api, errorText } from './api';
import type { Domain, RunPage, RunSummary } from './api';
import { Icon, runStatusLabel } from './AnalysisWorkspace';

function historyGroup(timestamp: string): string {
  const date = new Date(timestamp), today = new Date();
  const start = new Date(today.getFullYear(), today.getMonth(), today.getDate()).getTime();
  const day = new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
  const age = Math.round((start - day) / 86400000);
  return age === 0 ? '今天' : age === 1 ? '昨天' : age > 1 && age < 7 ? '近 7 天' : '更早';
}

export default function RecentRuns({ domains, selectedRunId, version, onOpen }: { domains: Domain[]; selectedRunId?: string; version: number; onOpen: (run: RunSummary) => void }) {
  const [search, setSearch] = useState('');
  const [query, setQuery] = useState('');
  const [domainId, setDomainId] = useState('');
  const [items, setItems] = useState<RunSummary[]>([]);
  const [offset, setOffset] = useState(0);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  useEffect(() => { const timer = setTimeout(() => { setQuery(search.trim()); setOffset(0); }, 220); return () => clearTimeout(timer); }, [search]);
  useEffect(() => {
    const controller = new AbortController(); setLoading(true); setError('');
    const params = new URLSearchParams({ limit: '20', offset: String(offset) });
    if (query) params.set('q', query); if (domainId) params.set('domain_id', domainId);
    api<RunPage>('/runs?' + params, { signal: controller.signal }).then(page => {
      if (controller.signal.aborted) return;
      setItems(current => offset === 0 ? page.items : [...current, ...page.items.filter(item => !current.some(old => old.run_id === item.run_id))]); setTotal(page.total);
    }).catch(err => { if (!controller.signal.aborted) { setError(errorText(err)); if (!offset) setItems([]); } }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [query, domainId, offset, version]);
  const groups = new Map<string, RunSummary[]>();
  for (const item of items) {
    const label = historyGroup(item.created_at);
    groups.set(label, [...(groups.get(label) || []), item]);
  }
  return <section className="recent-runs" aria-label="最近分析">
    <div className="history-search"><Icon name="search" size={17} /><input type="search" aria-label="搜索分析记录" placeholder="搜索历史对话…" value={search} onChange={event => setSearch(event.target.value)} /></div>
    <div className="recent-heading"><h2>历史对话</h2><select aria-label="历史记录业务域" value={domainId} onChange={event => { setDomainId(event.target.value); setOffset(0); }}><option value="">全部业务域</option>{domains.map(item => <option key={item.id} value={item.id}>{item.title}</option>)}</select></div>
    <div className="recent-list">{[...groups].map(([label, records]) => <div className="history-group" key={label}><h3>{label}</h3>{records.map(item => <button className={'recent-run ' + (selectedRunId === item.run_id ? 'selected' : '')} key={item.run_id} title={item.question} aria-current={selectedRunId === item.run_id ? 'true' : undefined} onClick={() => onOpen(item)}><strong>{item.question}</strong><span><time dateTime={item.created_at}>{new Date(item.created_at).toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' })} · {new Date(item.created_at).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}</time><span className={'history-status status-' + (item.partial ? 'partial' : item.status)}>{runStatusLabel(item.status, item.partial)}</span></span></button>)}</div>)}
      {error && <p className="recent-error" role="alert">{error}</p>}
      {loading && <p className="recent-placeholder"><span className="spinner tiny" />读取记录…</p>}
      {!loading && !error && !items.length && <p className="recent-placeholder">{query || domainId ? '没有匹配的分析记录' : '你的分析会保存在这里'}</p>}
      {items.length < total && <button className="text-button history-more" disabled={loading} onClick={() => setOffset(items.length)}>加载更多<Icon name="chevron" size={13} /></button>}
    </div>
  </section>;
}
