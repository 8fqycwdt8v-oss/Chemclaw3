import { readFileSync } from 'node:fs';
import { normalizeEvent } from './shared/events.ts';

const lines = readFileSync(
  '/tmp/claude-0/-home-user-Chemclaw3/41f2465f-44e8-5661-9ba7-5183da558c73/scratchpad/wire.jsonl',
  'utf8',
).trim().split('\n');

for (const line of lines) {
  const { case: name, sse_event, data } = JSON.parse(line) as Record<string, string>;
  const wire = JSON.parse(data) as Record<string, unknown>;
  const out = normalizeEvent(wire, sse_event);
  const lostFields = out
    ? Object.keys(wire).filter((k) => !(k in (out as Record<string, unknown>)))
    : Object.keys(wire);
  console.log(
    `${name.padEnd(20)} normalized=${out ? 'yes' : 'NULL'}  dropped=[${lostFields.join(',')}]`,
  );
}
