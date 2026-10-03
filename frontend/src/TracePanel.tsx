import { useEffect, useId, useRef } from 'react';
import { createPortal } from 'react-dom';
import type { RunEvent } from './api';
import { deriveProgress, eventPresentation, orderedRunEvents } from './presentation';

interface ProgressProps { events: RunEvent[]; status: string; partial?: boolean }
export function AnalysisProgress({ events, status, partial = false }: ProgressProps) {
  const progress = deriveProgress(events, status, partial);
  return <section className={'analysis-progress' + (progress.stages.length ? '' : ' status-only')} aria-label="分析进度">
    <p className={`progress-status is-${progress.tone}`} role="status">{progress.label}</p>
    {progress.stages.length > 0 && <ol className="progress-stages">
      {progress.stages.map((stage, index) => <li className={`progress-stage is-${stage.state}`} key={stage.label}
        aria-current={stage.state === 'active' || stage.state === 'paused' ? 'step' : undefined}>
        <span className="progress-index" aria-hidden="true">{stage.state === 'complete' ? '✓' : index + 1}</span>
        <span>{stage.label}</span>
      </li>)}
    </ol>}
  </section>;
}

interface TraceDrawerProps extends ProgressProps { connection: string; onClose: () => void }
export function TraceDrawer({ events, status, partial = false, connection, onClose }: TraceDrawerProps) {
  const titleId = useId();
  const panel = useRef<HTMLElement>(null);
  const backdrop = useRef<HTMLDivElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);
  const closeHandler = useRef(onClose);
  closeHandler.current = onClose;
  const ordered = orderedRunEvents(events);
  const progress = deriveProgress(events, status, partial);

  useEffect(() => {
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const previousOverflow = document.body.style.overflow;
    const background = [...document.body.children].filter((element): element is HTMLElement => element instanceof HTMLElement && element !== backdrop.current);
    const priorInert = background.map(element => [element, element.inert] as const);
    for (const [element] of priorInert) element.inert = true;
    document.body.style.overflow = 'hidden';
    closeButton.current?.focus({ preventScroll: true });
    function focusable() {
      return [...(panel.current?.querySelectorAll<HTMLElement>('button:not(:disabled), a[href], input:not(:disabled), textarea:not(:disabled), select:not(:disabled), summary, [tabindex]:not([tabindex="-1"])') || [])]
        .filter(element => element.getClientRects().length > 0 && !element.closest('[hidden]'));
    }
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') { event.preventDefault(); closeHandler.current(); return; }
      if (event.key !== 'Tab') return;
      const targets = focusable();
      const first = targets[0];
      const last = targets.at(-1);
      if (!first || !last) { event.preventDefault(); panel.current?.focus(); return; }
      if (!panel.current?.contains(document.activeElement) || (event.shiftKey && document.activeElement === first)) {
        event.preventDefault(); (event.shiftKey ? last : first).focus();
      } else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
    function onFocus(event: FocusEvent) {
      if (event.target instanceof Node && !panel.current?.contains(event.target)) closeButton.current?.focus({ preventScroll: true });
    }
    document.addEventListener('keydown', onKeyDown);
    document.addEventListener('focusin', onFocus);
    return () => {
      document.removeEventListener('keydown', onKeyDown);
      document.removeEventListener('focusin', onFocus);
      document.body.style.overflow = previousOverflow;
      for (const [element, inert] of priorInert) element.inert = inert;
      if (previousFocus?.isConnected) previousFocus.focus({ preventScroll: true });
    };
  }, []);

  return createPortal(<div className="trace-backdrop" ref={backdrop} onClick={event => {
    if (event.target === event.currentTarget) onClose();
  }}>
    <aside ref={panel} className="trace-drawer" role="dialog" aria-modal="true" aria-labelledby={titleId} tabIndex={-1}>
      <header className="trace-drawer-header">
        <h2 id={titleId}>分析过程</h2>
        <button type="button" className="trace-close" ref={closeButton} aria-label="关闭分析过程" title="关闭分析过程" onClick={onClose}>
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18" /></svg>
        </button>
      </header>
      <div className="trace-drawer-meta"><span>{progress.label}</span><span>{connection || '已保存'} · {ordered.length} 条记录</span></div>
      <div className="trace-events">
        {ordered.map(event => {
          const presentation = eventPresentation(event);
          const time = new Date(event.created_at);
          return <article className="trace-event" key={event.event_id}>
            <div className="trace-event-heading"><h3>{presentation.label}</h3>
              <time dateTime={event.created_at} title={Number.isNaN(time.getTime()) ? undefined : time.toLocaleString('zh-CN')}>
                {Number.isNaN(time.getTime()) ? '时间未记录' : time.toLocaleTimeString('zh-CN', { hour12: false })}
              </time>
            </div>
            <p className="trace-event-summary">{presentation.summary}</p>
            <details className="trace-event-payload"><summary>原始数据<span>{event.type}</span></summary>
              <pre tabIndex={0} aria-label={`${presentation.label}原始数据`}><code>{JSON.stringify(event.payload, null, 2)}</code></pre>
            </details>
          </article>;
        })}
        {!ordered.length && <p className="trace-empty">当前没有已保存的过程记录，以任务状态为准。</p>}
      </div>
    </aside>
  </div>, document.body);
}
