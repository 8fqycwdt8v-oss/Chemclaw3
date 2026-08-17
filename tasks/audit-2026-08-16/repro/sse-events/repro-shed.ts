/** turns.py:120 — the admission shed, verbatim off the wire. */
import { normalizeEvent } from './shared/events.ts';
import { errorFromEvent } from './src/api/errors.ts';

const shed = JSON.parse(
  '{"type":"error","message":"server at capacity; retry shortly","code":"budget_exhausted","retryable":true,"correlation_id":""}',
);
const e = normalizeEvent(shed)!;
console.log('backend said retryable =', (shed as { retryable: boolean }).retryable);
const err = errorFromEvent(e as never);
console.log('UI ApiError kind =', err.kind, '| retryable =', err.retryable);
console.log(
  'sendMessage.ts:251 branch =>',
  err.kind === 'budget_exhausted'
    ? "setComposerLock('budget_exhausted'); banner with NO retry action"
    : 'composer released',
);
