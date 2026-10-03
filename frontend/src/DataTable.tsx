import { useEffect, useMemo, useState } from 'react';
import type { Query, RowsPage } from './api';
import { api, cell, errorText } from './api';
import { formatCell } from './format';
import { rowsPath } from './resultData';

export default function DataTable({ query, runId }: { query: Query; runId?: string }) {
  const [offset, setOffset] = useState(0);
  const [limit, setLimit] = useState(10);
  const [sortBy, setSortBy] = useState('');
  const [descending, setDescending] = useState(false);
  const [page, setPage] = useState<RowsPage | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [localOnly, setLocalOnly] = useState(false);
  const savedRunId = query.result_ref?.run_id || runId;
  const savedQueryId = query.result_ref?.query_id || query.id;
  useEffect(() => { setOffset(0); setSortBy(''); setPage(null); setLocalOnly(false); setError(''); }, [savedRunId, savedQueryId]);
  useEffect(() => {
    if (!savedRunId || localOnly) return;
    const controller = new AbortController(); setLoading(true); setError('');
    const parameters = new URLSearchParams({ offset: String(offset), limit: String(limit), descending: String(descending) });
    if (sortBy) parameters.set('sort_by', sortBy);
    api<RowsPage>(`${rowsPath(savedRunId, savedQueryId)}?${parameters}`, { signal: controller.signal }).then(value => { if (!controller.signal.aborted) setPage(value); }).catch(error => {
      if (!controller.signal.aborted) { setError(`完整结果分页暂不可用，以下仅显示已有预览。${errorText(error)}`); setLocalOnly(true); setOffset(0); }
    }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [savedRunId, savedQueryId, offset, limit, sortBy, descending, localOnly]);
  const localRows = useMemo(() => {
    const rows = [...query.rows];
    if (sortBy) rows.sort((a, b) => {
      const left = cell(query, a, sortBy), right = cell(query, b, sortBy);
      if (left == null) return right == null ? 0 : 1;
      if (right == null) return -1;
      const result = typeof left === 'number' && typeof right === 'number' ? left - right : String(left).localeCompare(String(right), 'zh-CN', { numeric: true });
      return descending ? -result : result;
    });
    return rows;
  }, [query, sortBy, descending]);
  const remote = !!savedRunId && !localOnly;
  const current = remote && page ? { ...query, columns: page.columns, rows: page.rows } : { ...query, rows: localRows.slice(offset, offset + limit) };
  const total = remote && page ? page.total : query.rows.length;
  function sort(column: string) { setOffset(0); setDescending(sortBy === column ? !descending : false); setSortBy(column); }
  return <div className="saved-table" aria-busy={loading}>
    {error && <p className="table-warning" role="status">{error}</p>}
    <div className="table-scroll"><table><thead><tr>{current.columns.map(column => <th key={column} aria-sort={sortBy === column ? descending ? 'descending' : 'ascending' : 'none'}><button onClick={() => sort(column)} aria-label={`按 ${column} 排序`}>{column}<span>{sortBy === column ? descending ? '↓' : '↑' : '↕'}</span></button></th>)}</tr></thead><tbody>{current.rows.map((row, index) => <tr key={`${offset}-${index}`}>{current.columns.map(column => { const value = cell(current, row, column); return <td key={column} className={typeof value === 'number' ? 'numeric-cell' : ''} title={typeof value === 'number' ? String(value) : undefined}>{formatCell(value)}</td>; })}</tr>)}</tbody></table></div>
    {!loading && total === 0 && <p className="table-note">查询成功，没有符合条件的数据。</p>}
    <div className="table-pagination"><span>{loading ? '读取已保存结果…' : `共 ${total.toLocaleString('zh-CN')} 行 · ${total ? offset + 1 : 0}–${Math.min(offset + limit, total)}`}{query.truncated || page?.truncated ? ' · 源结果已截断，不能视为完整数据' : ''}{page?.legacy ? ' · 历史保存结果' : ''}</span><label>每页<select aria-label={`每页行数 ${query.id}`} value={limit} onChange={event => { setOffset(0); setLimit(Number(event.target.value)); }}>{[10, 25, 50, 100].map(value => <option key={value} value={value}>{value}</option>)}</select></label><button aria-label={`上一页 ${query.id}`} disabled={loading || offset === 0} onClick={() => setOffset(Math.max(0, offset - limit))}>上一页</button><button aria-label={`下一页 ${query.id}`} disabled={loading || offset + limit >= total} onClick={() => setOffset(offset + limit)}>下一页</button></div>
  </div>;
}
