import type { RunEvent } from './api';

export function mergeRunEvents(current: RunEvent[], incoming: RunEvent[]): RunEvent[] {
  const unique = new Map<number, RunEvent>();
  for (const event of [...current, ...incoming]) {
    const eventId = Number(event.event_id);
    if (!Number.isSafeInteger(eventId) || eventId < 0) continue;
    unique.set(eventId, { ...event, event_id: eventId });
  }
  return [...unique.values()].sort((a, b) => a.event_id - b.event_id);
}

export function lastEventCursor(events: RunEvent[]): number {
  return events.reduce((cursor, event) => {
    const eventId = Number(event.event_id);
    return Number.isSafeInteger(eventId) && eventId >= 0 ? Math.max(cursor, eventId) : cursor;
  }, 0);
}
