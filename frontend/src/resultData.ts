import { api } from './api';
import type { Query, RowsPage } from './api';

const completeResults = new Map<string, Promise<Query>>();
export function rowsPath(runId: string, queryId: string) {
  return `/runs/${encodeURIComponent(runId)}/queries/${encodeURIComponent(queryId)}/rows`;
}
export function needsFullResult(query: Query) { return !!query.result_ref && (query.total_rows ?? query.rows.length) > query.rows.length; }
export async function fullSavedResult(query: Query): Promise<Query> {
  if (!needsFullResult(query) || !query.result_ref) return query;
  const { run_id, query_id } = query.result_ref;
  const key = `${run_id}/${query_id}/${query.total_rows}/${query.result_bytes || ''}`;
  if (!completeResults.has(key)) {
    if (completeResults.size >= 12) completeResults.delete(completeResults.keys().next().value!);
    completeResults.set(key, api<RowsPage>(`${rowsPath(run_id, query_id)}?offset=0&limit=20000`).then(page => {
      if (page.total > page.rows.length) throw new Error('完整结果超过单次展示边界，请通过表格分页查看。');
      return { ...query, columns: page.columns, rows: page.rows, total_rows: page.total, truncated: page.truncated };
    }).catch(error => { completeResults.delete(key); throw error; }));
  }
  return completeResults.get(key)!;
}
