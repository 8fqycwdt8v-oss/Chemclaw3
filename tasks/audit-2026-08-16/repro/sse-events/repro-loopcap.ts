/** Feed the exact byte stream the backend sends for a loop-capped turn into streamTurn. */
import { streamTurn } from './src/api/streamTurn.ts';

const frames = [
  { event: 'token', data: '{"type":"token","text":"Partial answer so far.","agent":""}' },
  {
    event: 'error',
    data: '{"type":"error","message":"The turn reached its 25-iteration limit and stopped with work still open, so the answer below is partial (session s1).","code":"loop_cap_reached","retryable":false,"correlation_id":"c0ffee"}',
  },
  {
    event: 'answer',
    data: '{"type":"answer","text":"Partial answer so far.","confidence":null,"unsupported_claims":[],"review_required":false,"verified_by":null,"challenged":false,"review_hold_id":null}',
  },
];
const body = frames.map((f) => `event: ${f.event}\ndata: ${f.data}\n\n`).join('');

globalThis.fetch = (async () =>
  new Response(new TextEncoder().encode(body), {
    status: 200,
    headers: { 'content-type': 'text/event-stream' },
  })) as typeof fetch;

const seen: string[] = [];
try {
  const answer = await streamTurn({
    sessionId: '0'.repeat(32),
    message: 'hi',
    signal: new AbortController().signal,
    getToken: async () => null,
    onEvent: (e) => seen.push(e.type),
  });
  console.log('RESOLVED with answer:', JSON.stringify(answer));
} catch (err) {
  console.log('THREW:', (err as Error).name, '/ kind=', (err as { kind?: string }).kind);
  console.log('message:', (err as Error).message);
}
console.log('events delivered to the store:', seen);
