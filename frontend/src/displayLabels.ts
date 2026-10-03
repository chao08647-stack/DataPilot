import type { DataSource, Domain } from './api';

// Presentation-only names: never change persisted origin flags or model metadata.
export function sourceDisplayName(source: Pick<DataSource, 'name' | 'builtin'>): string {
  return source.builtin ? source.name.replace(/\s*·\s*合成数据\s*$/, '') : source.name;
}

export function domainDisplayDescription(domain: Domain): string {
  if (!domain.synthetic) return domain.description || '尚未填写说明';
  return domain.table_count
    ? `覆盖 ${domain.table_count} 张数据表，已配置 ${domain.metrics?.length || 0} 个指标。`
    : '基于已发布的指标口径，开展跨表查询与分析。';
}
