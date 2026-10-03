export type RecordValue = Record<string, unknown>;
export interface Example { id: string; question: string; title?: string; tags?: string[] }
export interface Domain { id: string; title: string; description: string; data_source_id: string; model_version?: number | string; date_range?: { start: string; end: string }; synthetic?: boolean; examples: Example[]; metrics: { id: string; name: string }[]; table_count?: number; tables?: TableMeta[]; relations?: RecordValue[]; dimensions?: RecordValue[]; dashboard_templates?: DashboardTemplate[] }
export type Scenario = Domain;
export interface Health { status: string; mode: string; postgres_ready: boolean; llm_configured: boolean; embedding_configured: boolean }
export interface RunEvent { event_id: number; run_id: string; type: string; payload: RecordValue; created_at: string }
export interface Query { id: string; title?: string; sql?: string; columns: string[]; rows: (unknown[] | RecordValue)[]; truncated?: boolean; derived?: boolean; recipe_output?: string; non_additive_columns?: string[]; result_ref?: { run_id: string; query_id: string }; total_rows?: number; result_bytes?: number }
export interface RowsPage { columns: string[]; rows: Query['rows']; total: number; offset: number; limit: number; truncated: boolean; legacy?: boolean; full_result_available?: boolean }
export type ChartType = 'bar' | 'line' | 'pie' | 'donut' | 'kpi' | 'table' | 'waterfall' | 'funnel' | 'heatmap' | 'horizontal_bar' | 'stacked_bar' | 'area' | 'scatter' | 'combo';
export interface ChartSeries { column: string; name?: string; unit?: string; type?: 'bar' | 'line' | 'area' | 'scatter'; axis?: 'left' | 'right'; stack?: string }
export interface Chart { type: ChartType; title: string; query_id: string; x?: string; x_name?: string; x_unit?: string; size?: string; y: string[]; series?: ChartSeries[]; unit?: string; value_divisor?: number; group_by?: string; start_value?: number; total_column?: string }
export interface Question { id: string; domain_id: string; title?: string; question: string; tags?: string[]; description?: string }
export interface RunSummary { run_id: string; thread_id: string; domain_id?: string; scenario_id?: string; question: string; status: string; origin?: string; question_id?: string; model_version?: string | number; created_at: string; updated_at?: string; error?: string | RecordValue | null; partial?: boolean }
export interface RunPage { items: RunSummary[]; total: number; limit: number; offset: number }
export type FindingKind = 'fact' | 'decomposition' | 'hypothesis' | 'insufficient_data';
export interface Analysis { summary: string; findings: { title: string; detail: string; evidence_ids?: string[]; kind?: FindingKind; business_objects?: string[] }[]; recommendations: string[]; limitations: string[]; follow_up_question?: string | null; evidence_gaps?: string[] }
export interface Investigation { steps?: { objective: string; metric_ids: string[]; grain: string; dimensions?: string[] }[]; evidence_needed?: string[]; diagnostic_ids?: string[]; period_selections?: RecordValue }
export interface Calculation { tool: string; method?: string; status?: string; facts?: { name: string; value: unknown; unit?: string; evidence_ids?: string[] }[]; reconciliation?: RecordValue; limitations?: string[]; evidence_ids?: string[] }
export interface Run { run_id: string; thread_id: string; domain_id?: string; scenario_id?: string; model_version?: number | string; question: string; status: string; created_at: string; error?: string | RecordValue | null; artifacts: { discovery?: RecordValue; investigation?: Investigation; calculations?: Calculation[]; partial?: boolean; queries?: Query[]; analysis?: Analysis; charts?: Chart[]; clarification?: string | RecordValue }; events?: RunEvent[] }
export interface Thread { thread_id: string; title?: string; question?: string; created_at?: string; updated_at?: string }
export interface ThreadDetail { thread?: Thread; messages: { role: string; content: string; run_id?: string }[]; runs: Run[] }
export interface Memory { id: string; domain_id?: string; scenario_id?: string; kind: 'business' | 'sql_experience' | 'preference'; title: string; content: string; active: boolean; origin?: string; metadata?: RecordValue; value?: unknown; key?: string }
export interface Skill { id: string; name: string; description: string; allowed_nodes?: string[]; scenarios?: string[]; version?: string }
export interface TableMeta { name: string; schema?: string; description?: string; grain?: string; columns: Record<string, string | RecordValue> | { name: string; type?: string; data_type?: string }[] }
export interface DataSource { id: string; name: string; kind: 'duckdb' | 'postgres' | 'mysql'; config: RecordValue; model_access: { metadata: boolean; results: boolean }; builtin?: boolean }
export interface SemanticModel { id: string; domain_id: string; version: number | string; status: 'draft' | 'published'; definition: RecordValue }
export interface DashboardTemplate { id: string; title: string; cards?: DashboardCard[] }
export interface Template { id: string; title: string; description?: string; table_count?: number; dashboard_templates?: DashboardTemplate[] }
export interface DashboardCard { id: string; title: string; chart: Partial<Chart> & { type: Chart['type'] }; query: { sql: string; metric_ids: string[]; filter_bindings: Record<string, string>; filter_grain?: 'month' | 'month_end' }; analysis_recipe?: { tool: string; config: RecordValue }; layout: { width: 6 | 12; order: number }; snapshot?: { query?: Query; queries?: Query[]; calculation?: Calculation; chart?: Chart; status: string; queried_at?: string; data_updated_at?: string | null; filters?: RecordValue; error?: string; stale?: boolean; analysis_generated_at?: string }; provenance?: { run_id: string; query_id: string } }
export interface Dashboard { id: string; title: string; domain_id: string; model_version?: number | string; filters: RecordValue; cards: DashboardCard[]; updated_at: string; last_refresh_at?: string }
export interface Drilldown { domain_id: string; question: string; context?: { dashboard_id?: string; card_id?: string; filters?: RecordValue; source_run_id?: string; query_id?: string } }

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}
export const API = '/api/v1';
export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API}${path}`, { ...init, headers: { 'Content-Type': 'application/json', ...init?.headers } });
  if (!response.ok) {
    let message = `${response.status} · ${response.statusText}`;
    try { const body = await response.json(); message = typeof body.detail === 'string' ? body.detail : body.detail ? JSON.stringify(body.detail) : body.message || message; } catch { /* Non-JSON proxy error. */ }
    throw new ApiError(response.status, message);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}
export function errorText(error: unknown): string {
  if (error instanceof ApiError && error.status === 503) return `服务尚未就绪：${error.message}。请检查服务配置；系统不会生成替代结果。`;
  return error instanceof Error ? error.message : '请求失败，请稍后重试。';
}
export function printable(value: unknown): string {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}
export function cell(query: Query, row: unknown[] | RecordValue, column: string): unknown {
  return Array.isArray(row) ? row[query.columns.indexOf(column)] : row[column];
}
