import { useEffect, useMemo, useRef, useState } from 'react';
import { init, use } from 'echarts/core';
import { BarChart, FunnelChart, HeatmapChart, LineChart, PieChart, ScatterChart } from 'echarts/charts';
import { DataZoomComponent, GridComponent, LegendComponent, TooltipComponent, VisualMapComponent } from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';
import type { EChartsOption } from 'echarts';
import type { Chart, Query } from './api';
import { cell, errorText } from './api';
import { buildChartOption, chartSeries } from './chartOptions';
import { formatCell, numericValue } from './format';
import { fullSavedResult, needsFullResult } from './resultData';
import DataTable from './DataTable';

use([BarChart, FunnelChart, HeatmapChart, LineChart, PieChart, ScatterChart, DataZoomComponent, GridComponent, LegendComponent, TooltipComponent, VisualMapComponent, CanvasRenderer]);

export default function ChartView({ chart, query }: { chart: Chart; query: Query }) {
  const host = useRef<HTMLDivElement>(null);
  const viewport = useRef<HTMLDivElement>(null);
  const [scrollable, setScrollable] = useState(false);
  const [saved, setSaved] = useState<Query | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  useEffect(() => {
    let active = true; setSaved(null); setError('');
    if (!needsFullResult(query) || chart.type === 'table') { setLoading(false); return; }
    setLoading(true);
    fullSavedResult(query).then(result => { if (active) setSaved(result); }).catch(error => { if (active) setError(errorText(error)); }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [query, chart.type]);
  const result = saved || query;
  const options = useMemo(() => ['table', 'kpi'].includes(chart.type) || !result.rows.length ? {} : buildChartOption(chart, result), [chart, result]);
  useEffect(() => {
    if (!host.current || !options.option || loading || error) return;
    const instance = init(host.current); instance.setOption(options.option as EChartsOption);
    const resize = () => { instance.resize(); setScrollable(!!viewport.current && viewport.current.scrollWidth > viewport.current.clientWidth + 1); };
    const observer = new ResizeObserver(resize); observer.observe(viewport.current || host.current); resize();
    return () => { observer.disconnect(); instance.dispose(); };
  }, [options, loading, error]);
  if (chart.type === 'table') return <DataTable query={query} />;
  if (!query.result_ref && (query.total_rows || 0) > query.rows.length) return <div className="chart-data-warning"><p>当前仅有结果预览，缺少完整保存结果的读取引用；未据此绘制完整图表。</p><DataTable query={query} /></div>;
  if (loading || needsFullResult(query) && !saved && !error) return <div className="chart-loading"><span className="spinner" />读取完整保存结果，不重新执行查询…</div>;
  if (error) return <div className="chart-data-warning"><p>无法读取完整图表数据：{error}。未使用预览冒充完整结果。</p><DataTable query={query} /></div>;
  if (!result.rows.length) return <div className="empty-state compact">没有符合当前条件的数据；未填充模拟数值。</div>;
  const fields = chartSeries(chart);
  if (chart.type === 'kpi') {
    if (fields.some(field => !result.columns.includes(field.column))) return <div className="chart-data-warning"><p>KPI 字段不存在，请查看原始查询结果。</p><DataTable query={result} /></div>;
    if (result.rows.length !== 1 || result.truncated || !fields.length) return <section className="chart-data-warning" aria-label="查询结果"><p><strong>查询结果（KPI 展示已停用）</strong><br />{result.truncated ? '查询结果已截断' : `查询返回 ${result.rows.length} 行`}，无法作为完整的单行 KPI 展示。以下不取首行、不求和；原图表标题不代表已完成指标汇总。</p><DataTable query={result} /></section>;
    return <div className="kpi-grid">{fields.map(field => { const raw = cell(result, result.rows[0], field.column); const number = numericValue(raw); const value = number === null ? null : number / (chart.value_divisor && chart.value_divisor > 0 ? chart.value_divisor : 1); return <div className="kpi" key={field.column}><span>{field.name || field.column}</span><strong>{formatCell(value)}<small>{field.unit || chart.unit || ''}</small></strong></div>; })}</div>;
  }
  if (options.warning) return <div className="chart-data-warning"><p>{options.warning}</p><DataTable query={result} /></div>;
  const categories = chart.type === 'horizontal_bar' ? new Set(result.rows.map(row => cell(result, row, chart.x || result.columns[0]))).size : 0;
  const minWidth = chart.type === 'heatmap' ? Math.max(640, new Set(result.rows.map(row => cell(result, row, chart.x || result.columns[0]))).size * 30 + 240) : undefined;
  const axisChart = !['pie', 'donut', 'funnel', 'heatmap'].includes(chart.type);
  return <>{categories > 15 && <p className="chart-window-note muted">共 {categories} 个分类，初始展示前 15 个；可滚动或拖动右侧滑块查看全部。仅调整视窗，不改变查询结果。</p>}{scrollable && <p className="chart-scroll-hint muted">图表较宽，可左右滚动查看全部坐标与数据。</p>}<div ref={viewport} className="chart-viewport" role="region" aria-label={chart.title + '图表视窗'} tabIndex={scrollable ? 0 : undefined}><div ref={host} className={'chart-canvas' + (categories > 15 ? ' long-ranking' : '') + (axisChart ? ' axis-chart' : '')} style={{ minWidth }} role="img" aria-label={chart.title} /></div></>;
}
