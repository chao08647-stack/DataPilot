import type { Chart, ChartSeries, Query } from './api';
import { numericValue } from './format';
import { chartPalette, palette } from './theme';

type Option = Record<string, unknown>;
export function chartSeries(chart: Chart): ChartSeries[] {
  return chart.series?.length ? chart.series : (chart.y || []).map(column => ({ column, name: column, unit: chart.unit, axis: 'left' }));
}
export function buildChartOption(chart: Chart, query: Query): { option?: Option; warning?: string } {
  const fields = chartSeries(chart);
  const x = chart.x || query.columns[0];
  const read = (row: Query['rows'][number], column: string) => Array.isArray(row) ? row[query.columns.indexOf(column)] : row[column];
  const label = (value: unknown) => value == null ? '未提供' : String(value);
  const divisor = chart.value_divisor && chart.value_divisor > 0 ? chart.value_divisor : 1;
  const number = (value: unknown) => { const result = numericValue(value); return result === null ? null : result / divisor; };
  const invalid = !fields.length || !query.columns.includes(x) || fields.some(field => !query.columns.includes(field.column)) || !!chart.group_by && !query.columns.includes(chart.group_by);
  if (invalid) return { warning: '图表缺少有效字段，请查看保存的数据。' };
  if (['pie', 'donut', 'funnel', 'waterfall', 'heatmap'].includes(chart.type) && fields.length !== 1) return { warning: '此图形需要一个明确的数值字段，未擅自丢弃其他指标；请查看原始数据。' };
  if (query.truncated) return { warning: '查询结果已截断，不能据此绘制完整分布或趋势；请查看已保存数据及查询边界。' };
  const strict = ['pie', 'donut', 'funnel', 'waterfall'].includes(chart.type);
  if (strict && query.rows.some(row => number(read(row, fields[0].column)) === null)) return { warning: '图表包含缺失或不可计算的值，不将缺失值补为 0。' };
  const option: Option = {
    color: [...chartPalette],
    textStyle: { fontFamily: 'Inter, Segoe UI, Microsoft YaHei, sans-serif', fontSize: 12, color: palette.secondary },
    tooltip: { trigger: ['pie', 'donut', 'funnel', 'heatmap', 'scatter'].includes(chart.type) ? 'item' : 'axis', renderMode: 'richText', backgroundColor: palette.surface, borderColor: palette.line, textStyle: { color: palette.ink, fontSize: 12 } },
    legend: { bottom: 0, type: 'scroll', icon: 'roundRect', itemWidth: 12, itemHeight: 8, textStyle: { fontSize: 12, color: palette.secondary } },
    grid: { top: 36, right: 35, bottom: 64, left: 64, containLabel: true },
  };
  const name = (field: ChartSeries) => field.name || field.column;
  if (['pie', 'donut', 'funnel'].includes(chart.type)) {
    if (['pie', 'donut'].includes(chart.type) && fields.some(field => query.non_additive_columns?.includes(field.column))) return { warning: '去重或非可加指标不能当作互斥构成展示，请查看原始查询结果。' };
    const data = query.rows.map(row => ({ name: label(read(row, x)), value: number(read(row, fields[0].column))! }));
    if (new Set(data.map(item => item.name)).size !== data.length) return { warning: '同一分类有多行结果，未擅自聚合；请核对统计粒度。' };
    if (data.some(item => item.value < 0)) return { warning: '饼图、环形图和漏斗不能表达负值，保留原始数据。' };
    option.series = chart.type === 'funnel' ? [{ type: 'funnel', name: [name(fields[0]), fields[0].unit || chart.unit].filter(Boolean).join(' · '), left: '12%', right: '12%', top: 12, bottom: 40, sort: 'none', gap: 4, minSize: '0%', label: { position: 'inside', color: '#fff', formatter: '{b}: {c}', fontSize: 12 }, data }] : [{ type: 'pie', name: [name(fields[0]), fields[0].unit || chart.unit].filter(Boolean).join(' · '), radius: chart.type === 'donut' ? ['40%', '66%'] : ['0%', '66%'], center: ['50%', '43%'], label: { formatter: '{b}', fontSize: 12, overflow: 'truncate', width: 100 }, itemStyle: { borderColor: '#fff', borderWidth: 2 }, data }];
    if (chart.type === 'funnel') option.legend = { show: false };
    return { option };
  }
  if (chart.type === 'heatmap') {
    if (!chart.group_by) return { warning: '热力图缺少第二维度，保留原始数据。' };
    const xs = [...new Set(query.rows.map(row => label(read(row, x))))];
    const groups = [...new Set(query.rows.map(row => label(read(row, chart.group_by!))))];
    const seen = new Set<string>(); const data: number[][] = [];
    for (const row of query.rows) {
      const xi = xs.indexOf(label(read(row, x))), yi = groups.indexOf(label(read(row, chart.group_by))); const key = `${xi}/${yi}`;
      if (seen.has(key)) return { warning: '同一维度组合有多行结果，未擅自聚合；请核对统计粒度。' };
      seen.add(key); const value = number(read(row, fields[0].column)); if (value !== null) data.push([xi, yi, value]);
    }
    option.xAxis = { type: 'category', data: xs, splitArea: { show: true } };
    option.yAxis = { type: 'category', data: groups, splitArea: { show: true }, axisLabel: { width: 160, overflow: 'break' } };
    option.grid = { top: 16, bottom: 85, left: 12, right: 20, containLabel: true };
    option.visualMap = { min: data.length ? Math.min(...data.map(item => item[2])) : 0, max: data.length ? Math.max(...data.map(item => item[2])) : 1, orient: 'horizontal', left: 'center', bottom: 0, calculable: true, inRange: { color: [palette.selected, '#AAC9FF', palette.primary, palette.primaryHover] } };
    option.series = [{ type: 'heatmap', name: [name(fields[0]), fields[0].unit || chart.unit].filter(Boolean).join(' · '), data, label: { show: data.length <= 60, fontSize: 12 }, itemStyle: { borderColor: '#fff', borderWidth: 2 } }]; option.legend = { show: false };
    return { option };
  }
  if (chart.type === 'waterfall') {
    let running = (chart.start_value || 0) / divisor;
    const base: number[] = [], up: number[] = [], down: number[] = [], totals: number[] = [];
    for (const row of query.rows) {
      const value = number(read(row, fields[0].column))!;
      const marker = chart.total_column ? read(row, chart.total_column) : false;
      if (marker === true || marker === 1 || marker === 'total') { running = value; base.push(0); totals.push(value); up.push(0); down.push(0); }
      else { const previous = running; running += value; base.push(Math.min(previous, running)); up.push(Math.max(value, 0)); down.push(Math.max(-value, 0)); totals.push(0); }
    }
    option.xAxis = { type: 'category', data: query.rows.map(row => label(read(row, x))), axisLabel: { hideOverlap: true } };
    option.yAxis = { type: 'value', name: fields[0].unit || chart.unit || '', splitLine: { lineStyle: { color: palette.line, type: 'dashed' } } };
    option.series = [{ name: '基线', data: base, itemStyle: { color: 'transparent' }, tooltip: { show: false } }, { name: '增加', data: up, itemStyle: { color: palette.primary } }, { name: '减少（绝对值）', data: down, itemStyle: { color: '#ec8c7d' } }, { name: '汇总', data: totals, itemStyle: { color: palette.muted } }].map(series => ({ ...series, type: 'bar', stack: 'waterfall', stackStrategy: 'all', barMaxWidth: 42 }));
    option.legend = { bottom: 0, data: ['增加', '减少（绝对值）', '汇总'] }; return { option };
  }
  const axisUnits = ['left', 'right'].map(axis => [...new Set(fields.filter(field => (field.axis || 'left') === axis).map(field => field.unit || chart.unit || '').filter(Boolean))]);
  if (axisUnits.some(units => units.length > 1)) return { warning: '同一坐标轴包含不同单位，请确认单位和左右轴配置；暂显示原始数据。' };
  const hasRightAxis = fields.some(field => field.axis === 'right');
  if (chart.type === 'horizontal_bar' && hasRightAxis) return { warning: '横向条形图不混用左右数值轴；请核对单位后查看原始数据。' };
  const valueAxes = [0, ...(hasRightAxis ? [1] : [])].map(index => ({ type: 'value', name: axisUnits[index][0] || '', position: index ? 'right' : 'left', splitLine: { show: !index, lineStyle: { color: palette.line, type: 'dashed' } } }));
  if (chart.type === 'scatter') {
    if (chart.size && !query.columns.includes(chart.size)) return { warning: '散点大小字段不存在，请查看原始数据。' };
    option.xAxis = { type: 'value', name: [chart.x_name || x, chart.x_unit].filter(Boolean).join(' · '), nameLocation: 'middle', nameGap: 30, splitLine: { lineStyle: { color: palette.line } } }; option.yAxis = valueAxes;
    const maxSize = chart.size ? Math.max(1, ...query.rows.map(row => Math.max(0, numericValue(read(row, chart.size!)) || 0))) : 1;
    option.series = fields.map(field => ({ name: name(field), type: 'scatter', yAxisIndex: field.axis === 'right' ? 1 : 0, symbolSize: chart.size ? (value: number[]) => 8 + 26 * Math.sqrt(value[2] / maxSize) : 10, dimensions: [[chart.x_name || x, chart.x_unit].filter(Boolean).join(' · '), [name(field), field.unit || chart.unit].filter(Boolean).join(' · '), chart.size || '大小'], encode: { x: 0, y: 1, tooltip: chart.size ? [0, 1, 2] : [0, 1] }, data: query.rows.flatMap(row => { const xv = numericValue(read(row, x)), yv = number(read(row, field.column)), size = chart.size ? numericValue(read(row, chart.size)) : 1; return xv === null || yv === null || size === null || size < 0 ? [] : [{ name: chart.group_by ? label(read(row, chart.group_by)) : '', value: [xv, yv, size] }]; }) }));
    return { option };
  }
  const categories = [...new Set(query.rows.map(row => label(read(row, x))))];
  const groups = chart.group_by ? [...new Set(query.rows.map(row => label(read(row, chart.group_by!))))] : [''];
  const indexed = new Map<string, Query['rows'][number]>();
  for (const row of query.rows) {
    const key = JSON.stringify([label(read(row, x)), chart.group_by ? label(read(row, chart.group_by)) : '']);
    if (indexed.has(key)) return { warning: '同一横轴与分组组合有多行结果，未擅自聚合；请核对维度和粒度。' };
    indexed.set(key, row);
  }
  const categoryAxis = { type: 'category', name: chart.x_name || '', data: categories, boundaryGap: !['line', 'area'].includes(chart.type), axisLabel: { hideOverlap: true }, axisTick: { show: false }, axisLine: { lineStyle: { color: palette.lineStrong } } };
  const horizontal = chart.type === 'horizontal_bar';
  option.xAxis = horizontal ? { ...valueAxes[0], position: 'bottom' } : categoryAxis;
  option.yAxis = horizontal ? { ...categoryAxis, inverse: true } : valueAxes;
  if (horizontal && categories.length > 15) {
    option.grid = { top: 36, right: 60, bottom: 64, left: 64, containLabel: true };
    option.dataZoom = [
      { type: 'slider', yAxisIndex: 0, right: 6, width: 16, top: 36, bottom: 64, startValue: 0, endValue: 14, filterMode: 'filter', showDetail: false },
      { type: 'inside', yAxisIndex: 0, startValue: 0, endValue: 14, filterMode: 'filter', zoomOnMouseWheel: false, moveOnMouseWheel: true, moveOnMouseMove: true },
    ];
  }
  option.series = groups.flatMap(group => fields.map((field, index) => {
    const renderType = field.type || (chart.type === 'line' || chart.type === 'area' ? 'line' : chart.type === 'combo' && index > 0 ? 'line' : 'bar');
    const line = renderType === 'line' || renderType === 'area';
    const values = categories.map(category => { const row = indexed.get(JSON.stringify([category, group])); return row ? number(read(row, field.column)) : null; });
    return { name: group ? fields.length === 1 ? group : `${group} · ${name(field)}` : name(field), type: line ? 'line' : renderType, data: values, yAxisIndex: horizontal ? 0 : field.axis === 'right' ? 1 : 0, stack: field.stack || (chart.type === 'stacked_bar' ? 'total' : undefined), connectNulls: false, ...(line ? { symbol: 'circle', symbolSize: 6, smooth: false, lineStyle: { width: 2.5 }, ...(chart.type === 'area' || renderType === 'area' ? { areaStyle: { opacity: .12 } } : {}) } : { barMaxWidth: 38, itemStyle: { borderRadius: [3, 3, 0, 0] } }) };
  }));
  return { option };
}
