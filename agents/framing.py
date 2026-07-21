"""Frame retrieved third-party content so the model reads it as data, not instructions.

Why this exists: note bodies and ELN-ingested reaction labels are not authored by the agent,
and not all of them pass the human PR-gate before they reach the model — agent-authored notes
do, but *ingested* ELN/ORD notes and fingerprint labels are third-party text that lands in the
graph directly. A note body containing "ignore your instructions and …" is the classic
indirect prompt-injection vector (the retrieval tools feed these bodies verbatim into context).

Wrapping retrieved content in an explicit, named envelope — paired with the agent instruction
that envelope contents are evidence to cite, never commands — is the cheap, centralized
mitigation. It does not neutralize a determined attacker, but it removes the trivial injection
and marks the trust boundary in one place (the two retrieval tools), rather than trusting each
tool to remember. Full content-provenance handling is a Phase-6 item (see DEFERRED).
"""

_OPEN = '<retrieved-note id="{note_id}">'
_CLOSE = "</retrieved-note>"


def frame_untrusted(content: str, *, note_id: str) -> str:
    """Wrap retrieved `content` from note `note_id` in a data envelope for the model.

    The envelope names the source note (so a citation is still obvious) and marks the span
    as retrieved data. The agent instructions tell the model that anything inside such an
    envelope is evidence to weigh and cite, not an instruction to obey.
    """
    return f"{_OPEN.format(note_id=note_id)}\n{content}\n{_CLOSE}"
