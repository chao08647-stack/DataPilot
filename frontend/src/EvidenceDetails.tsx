import type { Calculation, FindingKind, Investigation } from './api';
import { printable } from './api';

const KINDS: Record<FindingKind, string> = { fact: '已观察事实', decomposition: '计算分解', hypothesis: '待验证假设', insufficient_data: '证据不足' };
export function FindingLabel({ kind }: { kind?: FindingKind }) {
  return <span className={`finding-kind ${kind || 'unclassified'}`}>{kind ? KINDS[kind] || kind : '未标注类型'}</span>;
}
export function InvestigationPanel({ investigation }: { investigation?: Investigation }) {
  if (!investigation?.steps?.length && !investigation?.evidence_needed?.length) return null;
  return <details className="investigation-panel"><summary>分析计划 · {investigation.steps?.length || 0} 个步骤<span>问题 → 指标与粒度 → 证据</span></summary><ol>{investigation.steps?.map((step, index) => <li key={index}><strong>{step.objective}</strong><p>统计粒度：{step.grain} · 指标：{step.metric_ids.join('、') || '待确定'}</p>{step.dimensions?.length ? <p>维度：{step.dimensions.join('、')}</p> : null}</li>)}</ol>{investigation.evidence_needed?.length ? <section><h4>需要核对的证据</h4><ul>{investigation.evidence_needed.map((item, index) => <li key={index}>{item}</li>)}</ul></section> : null}{investigation.diagnostic_ids?.length ? <p className="plan-diagnostics">已选择分析方法：{investigation.diagnostic_ids.join('、')}</p> : null}{investigation.period_selections && Object.keys(investigation.period_selections).length > 0 && <pre>{JSON.stringify(investigation.period_selections, null, 2)}</pre>}</details>;
}
export function CalculationPanels({ calculations }: { calculations?: Calculation[] }) {
  if (!calculations?.length) return null;
  return <div className="calculation-panels">{calculations.map((item, index) => <details className="calculation-panel" key={`${item.tool}-${index}`}><summary>计算证据 · {item.tool}<span>{item.status || '查看方法与限制'}</span></summary><p className="calculation-method">{item.method || '未提供方法说明'}</p>{item.facts?.length ? <dl className="calculation-facts">{item.facts.map((fact, factIndex) => <div key={factIndex}><dt>{fact.name}</dt><dd>{printable(fact.value)}{fact.unit ? ` ${fact.unit}` : ''}</dd>{fact.evidence_ids?.length ? <small>依据：{fact.evidence_ids.join('、')}</small> : null}</div>)}</dl> : null}{item.reconciliation && Object.keys(item.reconciliation).length > 0 && <details className="calculation-reconciliation"><summary>核对与勾稽结果</summary><pre>{JSON.stringify(item.reconciliation, null, 2)}</pre></details>}{item.limitations?.length ? <div className="calculation-limitations"><strong>适用边界</strong><ul>{item.limitations.map((text, limitIndex) => <li key={limitIndex}>{text}</li>)}</ul></div> : null}{item.evidence_ids?.length ? <p className="plan-diagnostics">查询证据：{item.evidence_ids.join('、')}</p> : null}</details>)}</div>;
}
