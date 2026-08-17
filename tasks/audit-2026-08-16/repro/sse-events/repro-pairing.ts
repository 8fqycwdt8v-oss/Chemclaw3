/** Root and helper both call gather_evidence; the helper answers first. */
import { useChatStore } from './src/state/chatStore.ts';
import { normalizeEvent } from './shared/events.ts';

const cid = useChatStore.getState().createConversation();
const mid = useChatStore.getState().startAssistantMessage(cid);

const wire = [
  { type: 'tool_call', tool: 'gather_evidence', arguments: '{"q":"ROOT question"}', agent: '' },
  { type: 'tool_call', tool: 'gather_evidence', arguments: '{"q":"HELPER question"}', agent: 'subagent' },
  // the helper's result comes back first, stamped agent="subagent"
  { type: 'tool_result', tool: 'gather_evidence', preview: 'HELPER RESULT', note_ids: ['N-helper'],
    numbers: [1], result_ref: 'h', agent: 'subagent' },
  { type: 'tool_result', tool: 'gather_evidence', preview: 'ROOT RESULT', note_ids: ['N-root'],
    numbers: [2], result_ref: 'r', agent: '' },
];
for (const raw of wire) {
  const e = normalizeEvent(raw);
  if (e) useChatStore.getState().applyEvent(cid, mid, e);
}
const msg = useChatStore.getState().conversations[cid]!.messages.find((m) => m.id === mid)!;
if (msg.role === 'assistant')
  for (const entry of msg.trace)
    if (entry.kind === 'tool_call')
      console.log(
        `call agent=${JSON.stringify(entry.toolCall!.agent)} args=${entry.toolCall!.arguments}` +
          ` -> result=${JSON.stringify(entry.toolCall!.result)} ref=${entry.toolCall!.resultRef}`,
      );
