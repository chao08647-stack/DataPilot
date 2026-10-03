import { lazy, Suspense } from 'react';
import type { Chart, Query } from './api';

const ChartView = lazy(() => import('./ChartView'));
export default function LazyChart(props: { chart: Chart; query: Query }) {
  return <Suspense fallback={<div className="loading-state"><span className="spinner" />加载图表组件…</div>}><ChartView {...props} /></Suspense>;
}
