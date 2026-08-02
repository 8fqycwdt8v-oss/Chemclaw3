"""Frame retrieved third-party content so the model reads it as data, not instructions.

Why this exists: note bodies, ELN-ingested reaction labels and uploaded attachments are not
authored by the agent, and not all of them pass the human PR-gate before they reach the model —
agent-authored notes do, but *ingested* ELN/ORD notes, fingerprint labels and a chemist's uploads
are third-party text that lands in context directly. A body containing "ignore your instructions
and …" is the classic indirect prompt-injection vector (the retrieval and attachment tools feed
these bodies verbatim into context).

Wrapping retrieved content in an explicit, named envelope — paired with the agent instruction
that envelope contents are evidence to cite, never commands — is the cheap, centralized
mitigation. Three forgery paths are closed *here* rather than at each call site, because a caller
that has to remember an escaping rule is a caller that will forget it (the attachment tools did):

- **The delimiter carries a per-process random nonce**, and the agent instructions name only the
  nonce'd tag as authoritative — so content cannot close an envelope whose tag it cannot guess,
  however it spells a lookalike (`</RETRIEVED-NOTE>`, `</ retrieved-note>`). Per-process rather
  than per-turn deliberately: the instructions are built once per process
  (`chemclaw_agent._INSTRUCTIONS`), and a durable session re-reads earlier turns' framed tool
  results, which must still be recognizable as envelopes then — a per-turn nonce would orphan
  every envelope already in history.
- **Any literal `<retrieved-note` in the content is defanged** (its `<` becomes `&lt;`), so even
  an exactly-reproduced live delimiter — echoed by the model into a visible answer, say, and fed
  back inside a later upload — is data by the time it is framed. The mechanism does not rest on
  the nonce staying secret, and the nonce does not rest on this pattern matching every spelling;
  each covers the other's gap.
- **The id attribute is reduced to a safe charset**, so a caller-supplied identifier (an uploaded
  file's name) can never close the opening tag from inside it.

This is the "escaped or randomized delimiters" escalation `verifier._verifier_prompt` said the
envelope must make when a source carrying untrusted external text lands; attachments are that
source. Full content-provenance handling remains a Phase-6 item (see DEFERRED).
"""

import re
import secrets

# Rotates on process start; stable across every turn a process serves, so framed content in a
# durable session's history keeps matching the instructions that vouch for it (module docstring).
_NONCE = secrets.token_hex(8)

# The one authoritative envelope tag. Public because `chemclaw_agent._INSTRUCTIONS` must name
# exactly this tag — the instruction and the delimiter drifting apart would silently unmark every
# envelope — and a shared constant is what lets a test pin the two together.
ENVELOPE_TAG = f"retrieved-note-{_NONCE}"

# Any `<` that begins a retrieved-note-like tag (open or close, any case, any nonce suffix,
# whitespace-padded or not). Matching the *prefix* rather than a full tag is deliberate: the goal
# is that no spelling of the tag survives into content, not to parse markup.
_FORGERY = re.compile(r"<(?=\s*/?\s*retrieved-note)", re.IGNORECASE)

# Everything an id may carry. Excludes `"`, `<` and `>` (so an id cannot terminate the attribute
# or the tag) while keeping the shapes real ids use: note slugs, `attachment:file.pdf`,
# `job-results`.
_ID_UNSAFE = re.compile(r"[^A-Za-z0-9._:-]")


def frame_untrusted(content: str, *, note_id: str) -> str:
    """Wrap retrieved `content` from source `note_id` in a data envelope for the model.

    The envelope names the source (so a citation is still obvious) and marks the span as
    retrieved data. The agent instructions tell the model that anything inside the — nonce'd,
    hence unforgeable — envelope is evidence to weigh and cite, not an instruction to obey.
    Content and id are neutralized as the module docstring describes, so neither can close the
    envelope early; the text is otherwise preserved verbatim.
    """
    safe_id = _ID_UNSAFE.sub("_", note_id) or "unknown"
    body = _FORGERY.sub("&lt;", content)
    return f'<{ENVELOPE_TAG} id="{safe_id}">\n{body}\n</{ENVELOPE_TAG}>'
