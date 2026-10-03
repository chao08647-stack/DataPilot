// Test-only fixtures; never imported by the application or used as runtime fallbacks.
export const domains = [
  { id: 'supply-chain', title: '供应链与履约', description: '采购、库存、运输的测试业务域', data_source_id: 'fixture-duckdb', synthetic: true, model_version: 2, metrics: [{ id: 'net', name: '净额' }], examples: [{ id: 'baseline', question: '比较各地区履约成本', title: '履约成本对比' }], dashboard_templates: [{ id: 'ops', title: '履约诊断模板' }] },
  { id: 'finance-custom', title: '财务分析自定义域', description: '没有预置行业约束', data_source_id: 'fixture-pg', model_version: 1, metrics: [], examples: [] },
];
const now = '2026-09-30T08:00:00Z';
const queries = [
  { id: 'q1', sql: 'SELECT region, value FROM approved_costs', columns: ['region', 'value', 'total'], rows: [['期初', 100, true], ['增加', 40, false], ['减少', -10, false], ['期末', 130, true]] },
  { id: 'q2', sql: 'SELECT stage, value FROM approved_funnel', columns: ['stage', 'value'], rows: [['提交', 100], ['批准', 80], ['完成', 65]] },
  { id: 'q3', sql: 'SELECT month, region, value FROM approved_matrix', columns: ['month', 'region', 'value'], rows: [['六月', '华东', 30], ['七月', '华东', 40], ['六月', '华南', 20], ['七月', '华南', 50]] },
];
const charts = [
  { title: '成本变动', type: 'waterfall', x: 'region', y: ['value'], query_id: 'q1', total_column: 'total' },
  { title: '履约漏斗', type: 'funnel', x: 'stage', y: ['value'], query_id: 'q2' },
  { title: '区域热力', type: 'heatmap', x: 'month', group_by: 'region', y: ['value'], query_id: 'q3' },
];
export const completed = { run_id: 'run-existing', thread_id: 'thread-existing', domain_id: 'supply-chain', model_version: 2, question: '比较各地区履约成本', status: 'completed', created_at: now, artifacts: { queries, charts, analysis: { summary: '测试数据中的净变化为 30，不代表真实业务结论。', findings: [], recommendations: [], limitations: ['浏览器测试专用固定数据'] } }, events: [{ event_id: 1, type: 'run.completed', run_id: 'run-existing', created_at: now, payload: { message: '测试任务已完成' } }] };
export function makeBoard(id = 'board-existing') {
  return { id, title: '履约经营看板', domain_id: 'supply-chain', model_version: 2, filters: {}, updated_at: now, cards: charts.map((chart, index) => ({ id: `card-${index}`, title: chart.title, chart, query: { sql: queries[index].sql, metric_ids: ['net'], filter_bindings: { region: 'region' }, filter_grain: 'month' }, layout: { width: index === 2 ? 12 : 6, order: index }, snapshot: { status: 'completed', query: queries[index], chart, filters: {}, queried_at: now, data_updated_at: null }, provenance: { run_id: 'run-existing', query_id: queries[index].id } })) };
}
export async function enterpriseApi(page, options = {}) {
  const requests = [];
  const state = {
    domains: structuredClone(domains), boards: options.emptyBoards ? [] : structuredClone([makeBoard()]),
    sources: [
      { id: 'fixture-duckdb', name: '合成测试数据库', kind: 'duckdb', config: { path: 'test-only.duckdb' }, builtin: true, model_access: { metadata: true, results: true } },
      { id: 'fixture-pg', name: '企业只读库', kind: 'postgres', config: { database: 'analytics' }, builtin: false, model_access: { metadata: false, results: false } },
    ],
    models: [{ id: 'model-2', domain_id: 'supply-chain', version: 2, status: 'published', definition: { id: 'supply-chain', tables: [{ name: 'approved_costs', columns: { region: 'VARCHAR', value: 'DOUBLE' }, grain: '地区成本' }], metrics: [{ id: 'net', formula: 'SUM(value)' }], relations: [], dimensions: [] } }],
    memory: [{ id: 'pref1', domain_id: 'supply-chain', kind: 'preference', title: '月度展示模板', content: '使用月度图表', active: false, origin: 'builtin', key: 'chart', value: 'line', metadata: { key: 'chart', value: 'line' } }],
    run: structuredClone(completed), additionalRuns: [], fullRows: {}, partialFailure: false, streamCalls: 0,
  };
  state.domains[0].dimensions = [{ id: 'region', table: 'approved_costs', column: 'region', type: 'category' }];
  await page.route('**/api/v1/**', async route => {
    const request = route.request(); const method = request.method(); const url = new URL(request.url()); const path = url.pathname.replace('/api/v1', '');
    const body = request.postData() ? JSON.parse(request.postData()) : undefined;
    requests.push({ path, method, body, query: url.search });
    const send = (value, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) });
    if (path === '/health') return send({ status: 'ok', mode: 'live', postgres_ready: !options.unready, llm_configured: true, embedding_configured: true });
    if (path === '/questions') return send([{ id: 'costs', title: '地区履约成本对比', question: '比较各地区履约成本', domain_id: 'supply-chain' }]);
    if (path === '/runs' && method === 'GET') {
      const all = [state.run, ...state.additionalRuns].filter(run => (!url.searchParams.get('domain_id') || run.domain_id === url.searchParams.get('domain_id')) && (!url.searchParams.get('q') || run.question.includes(url.searchParams.get('q'))));
      const offset = Number(url.searchParams.get('offset') || 0), limit = Number(url.searchParams.get('limit') || 20);
      return send({ items: all.slice(offset, offset + limit).map(({ artifacts, events, ...run }) => ({ ...run, partial: !!artifacts?.partial })), total: all.length, offset, limit });
    }
    if (path === '/domains' && method === 'GET') return send(state.domains);
    if (path === '/domains' && method === 'POST') { const domain = { ...body, synthetic: false, examples: [], metrics: [], model_version: null }; state.domains.push(domain); return send(domain); }
    if (path.startsWith('/domains/')) return send(state.domains.find(domain => domain.id === path.split('/')[2]) || {});
    if (path === '/templates') return send([{ id: 'supply-chain', title: '供应链示例模板', description: '测试模板', dashboard_templates: domains[0].dashboard_templates }]);
    if (path.endsWith('/install')) return send(state.domains[0]);
    if (path === '/data-sources' && method === 'GET') return send(state.sources);
    if (path === '/data-sources' && method === 'POST') { state.sources.push({ ...body, builtin: false }); return send(state.sources.at(-1)); }
    if (path.startsWith('/data-sources/') && path.endsWith('/introspect')) return send([{ name: 'approved_costs', columns: [{ name: 'region', data_type: 'VARCHAR' }, { name: 'value', data_type: 'DOUBLE' }] }]);
    if (path.startsWith('/data-sources/') && path.endsWith('/test')) return send({ status: 'ok' });
    if (path.startsWith('/data-sources/') && method === 'PATCH') { const source = state.sources.find(item => item.id === path.split('/')[2]); Object.assign(source, body); return send(source); }
    if (path === '/semantic-models' && method === 'GET') return send(state.models.filter(model => model.domain_id === url.searchParams.get('domain_id')));
    if (path === '/semantic-models' && method === 'POST') { const model = { ...body, id: `draft-${state.models.length}`, version: state.models.length + 2, status: 'draft' }; state.models.push(model); return send(model); }
    if (path.startsWith('/semantic-models/') && path.endsWith('/publish')) { const model = state.models.find(item => item.id === path.split('/')[2]); model.status = 'published'; return send(model); }
    if (path === '/dashboards' && method === 'GET') return send(state.boards.filter(board => board.domain_id === url.searchParams.get('domain_id')));
    if (path === '/dashboards' && method === 'POST') { const board = { ...makeBoard(`board-${state.boards.length + 1}`), title: body.title, domain_id: body.domain_id, cards: body.template_id ? makeBoard().cards.map(({ snapshot, ...card }) => card) : [] }; state.boards.push(board); return send(board); }
    if (path.startsWith('/dashboards/')) {
      const parts = path.split('/'); const board = state.boards.find(item => item.id === parts[2]);
      if (!board) return send({ detail: 'not found' }, 404);
      if (parts[3] === 'refresh') { board.filters = body.filters; board.last_refresh_at = now; board.cards = board.cards.map((card, index) => ({ ...card, snapshot: state.partialFailure && index === 0 ? { ...card.snapshot, stale: true, error: '月度卡片必须选择完整月份', status: 'failed' } : { status: 'completed', query: queries[index], chart: charts[index], queried_at: now, filters: body.filters, data_updated_at: null } })); return send(board); }
      if (parts[3] === 'cards' && parts[5] === 'drilldown') return send({ domain_id: board.domain_id, question: `继续分析 ${body.dimension}=${body.value}`, context: { dashboard_id: board.id, card_id: parts[4], filters: board.filters, source_run_id: 'run-existing', query_id: 'q1' } });
      if (parts[3] === 'cards' && method === 'POST') { board.cards.push({ ...makeBoard().cards[0], id: `pinned-${board.cards.length}` }); return send(board); }
      if (method === 'PATCH') { Object.assign(board, body); return send(board); }
      if (method === 'DELETE') { state.boards = state.boards.filter(item => item.id !== board.id); return route.fulfill({ status: 204 }); }
      return send(board);
    }
    if (path.startsWith('/scenarios/')) return send({ ...completed, run_id: 'replay-baseline', thread_id: '', events: [] });
    if (path === '/threads') return send(url.searchParams.get('domain_id') === 'supply-chain' ? [{ thread_id: 'thread-existing', title: '已有履约分析', updated_at: now }] : []);
    if (path.startsWith('/threads/')) return send({ messages: [], runs: [state.run] });
    if (path === '/runs' && method === 'POST') { if (options.unready || options.rejectRuns) return send({ detail: 'PostgreSQL unavailable' }, 503); state.run = { ...state.run, question: body.question, status: 'waiting_for_input', artifacts: { clarification: { question: '采用哪个时间口径？' } }, events: [] }; return send(state.run); }
    if (path.endsWith('/resume')) { state.run.status = 'running'; return send(state.run); }
    if (path.endsWith('/events')) {
      state.streamCalls += 1; state.run = structuredClone(completed);
      return route.fulfill({ status: 200, contentType: 'text/event-stream', body: 'id: 1\nevent: run.completed\ndata: ' + JSON.stringify(completed.events[0]) + '\n\nid: 1\nevent: run.completed\ndata: ' + JSON.stringify(completed.events[0]) + '\n\nevent: stream.closed\ndata: {}\n\n' });
    }
    if (path.match(/^\/runs\/[^/]+\/queries\/[^/]+\/rows$/)) {
      const parts = path.split('/'), run = [state.run, ...state.additionalRuns].find(run => run.run_id === parts[2]);
      const query = run?.artifacts.queries?.find(query => query.id === parts[4]);
      if (!query) return send({ detail: 'Missing result' }, 404);
      let rows = structuredClone(state.fullRows[query.id] || query.rows);
      const column = url.searchParams.get('sort_by'), descending = url.searchParams.get('descending') === 'true';
      const value = row => Array.isArray(row) ? row[query.columns.indexOf(column)] : row[column];
      if (column) rows.sort((a, b) => { const left = value(a), right = value(b); if (left == null) return right == null ? 0 : 1; if (right == null) return -1; const result = typeof left === 'number' && typeof right === 'number' ? left - right : String(left).localeCompare(String(right)); return descending ? -result : result; });
      const offset = Number(url.searchParams.get('offset') || 0), limit = Number(url.searchParams.get('limit') || 10);
      return send({ columns: query.columns, rows: rows.slice(offset, offset + limit), total: rows.length, offset, limit, truncated: !!query.truncated });
    }
    if (path.startsWith('/runs/')) { const run = [state.run, ...state.additionalRuns].find(run => run.run_id === path.split('/')[2]); if (!run) return send({ detail: 'not found' }, 404); if (method === 'DELETE') run.status = 'cancelled'; return send(run); }
    if (path === '/memories') return send(state.memory);
    if (path.startsWith('/memories/')) { if (method === 'DELETE') { state.memory = []; return route.fulfill({ status: 204 }); } Object.assign(state.memory[0], body); return send(state.memory[0]); }
    if (path === '/skills') return send([{ id: 'scope', name: '跨表粒度检查', description: '测试技能：核对业务粒度', allowed_nodes: ['discovery', 'sql'], version: '1' }]);
    return send({ detail: `Unmocked ${method} ${path}` }, 404);
  });
  return { state, requests };
}
