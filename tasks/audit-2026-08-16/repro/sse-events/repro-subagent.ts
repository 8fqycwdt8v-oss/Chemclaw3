/** The store's view of a turn in which the model delegated to the `task` helper. */
import { useChatStore } from './src/state/chatStore.ts';
import { normalizeEvent } from './shared/events.ts';

const s = useChatStore.getState();
const cid = s.createConversation();
const mid = useChatStore.getState().startAssistantMessage(cid);

// Exactly what api/graph_stream.py yields: root tokens unattributed, helper tokens agent="subagent".
const wire = [
  { type: 'token', text: 'Checking the ELN. ', agent: '' },
  { type: 'token', text: '[helper] I will grep three notes and summarise…', agent: 'subagent' },
  { type: 'token', text: 'The batch used 2.0 eq of base.', agent: '' },
  // api/runner.py:311 concatenates ONLY the unattributed ones into answer.text
  {
    type: 'answer',
    text: 'Checking the ELN. The batch used 2.0 eq of base.',
    confidence: null, unsupported_claims: [], review_required: false, verified_by: null,
  },
];
for (const raw of wire) {
  const e = normalizeEvent(raw);
  if (e) useChatStore.getState().applyEvent(cid, mid, e);
}
const msg = useChatStore.getState().conversations[cid]!.messages.find((m) => m.id === mid)!;
if (msg.role === 'assistant') {
  console.log('streamedText (what the chemist read while it ran):');
  console.log('  ', JSON.stringify(msg.streamedText));
  console.log('finalText   (what replaces it at the answer):');
  console.log('  ', JSON.stringify(msg.finalText));
  console.log('identical:', msg.streamedText === msg.finalText);
}
