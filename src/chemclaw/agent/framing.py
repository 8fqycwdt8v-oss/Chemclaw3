"""Frame retrieved third-party content so the model reads it as data, not instructions.

Note bodies, ELN labels and uploads reach the model unreviewed, so they are an indirect
prompt-injection vector. Content goes inside a named envelope that the agent instructions call
evidence, never commands. Forgery is closed here, not at call sites:

- The delimiter carries a per-deployment (or per-process) nonce, so content cannot guess it.
- Any literal `<retrieved-note` in content is escaped, so even an exact copy of the live
  delimiter is data. Nonce and escape each cover the other's gap.
- The id attribute is reduced to a safe charset, so an id cannot close the opening tag.
- Any `[system <nonce>]`-shaped span is escaped, protecting `SYSTEM_SPEECH_MARK`, the second
  trust anchor.
"""

import hmac
import re
import secrets
import sys
import unicodedata
from hashlib import sha256

from chemclaw.core.config import settings


def _envelope_nonce() -> str:
    """The tag suffix: stable for the whole deployment when configured, else per process.

    A durable session outlives a process, and an envelope with an unrecognized nonce is read as
    ordinary content, so production must set `framing_envelope_secret` (settings warn when
    `session_store="postgres"` lacks it). The secret is hashed so it never appears in a prompt or
    stored row. Unset falls back to a per-process random value, as dev and tests want.
    """
    secret = settings.framing_envelope_secret.get_secret_value()
    if secret:
        return hmac.new(secret.encode(), b"chemclaw-retrieved-note-envelope", sha256).hexdigest()[
            :16
        ]
    return secrets.token_hex(8)


_NONCE = _envelope_nonce()

# The one authoritative envelope tag; public so the agent instructions and tests name exactly it.
ENVELOPE_TAG = f"retrieved-note-{_NONCE}"

# Marks a sentence in a tool result as this system's own (e.g. a refusal), not a tool's words.
#
# A value rather than a spelling, so text from the far side of a tool call cannot forge it; it
# reuses the envelope nonce, so one secret configures both. Appended rather than prefixed so
# readers keyed on the `Refused: ` prefix keep working; `bounded_content` keeps head and tail, so
# the mark survives truncation.
SYSTEM_SPEECH_MARK = f"[system {_NONCE}]"

# Any `<` beginning a retrieved-note-like tag (open or close, any case or suffix, padded or not).
# Matching the prefix means no spelling of the tag survives, without parsing markup.
_FORGERY = re.compile(r"<(?=\s*/?\s*retrieved-note)", re.IGNORECASE)

# Any `[` beginning a `[system <hex>]`-shaped span. Shape-matched, unlike `_FORGERY`: "system" is
# an ordinary word ("[system pressure 3 bar]"), so the lookahead requires a hex run of at least
# half the nonce's length and the closing bracket, which still catches a guessed or cut nonce.
_MARK_FORGERY = re.compile(r"\[(?=\s*system\s+[0-9a-f]{8,}\s*\])", re.IGNORECASE)

# A `str.translate` table deleting every Unicode format (`Cf`) character. They render as nothing,
# so they can disguise a tag from `_FORGERY`; deriving the set from the category keeps it total
# across Unicode revisions.
_INVISIBLE = dict.fromkeys(
    cp for cp in range(sys.maxunicode + 1) if unicodedata.category(chr(cp)) == "Cf"
)


def _defang(content: str) -> str:
    """Neutralize every spelling of this deployment's two trust anchors inside `content`.

    Each anchor gets a direct substitution, then a check on a copy with invisible characters
    removed: if that reveals an anchor, the content is obfuscated and every `<` (tag) or `[` (mark)
    that could begin one is escaped. The two blunt passes stay separate so one attack does not
    corrupt the other channel's evidence. Invisible characters are not stripped from the output, to
    keep evidence faithful.
    """
    revealed = content.translate(_INVISIBLE)
    body = _FORGERY.sub("&lt;", content)
    if _FORGERY.search(revealed):
        body = body.replace("<", "&lt;")
    return _marks_escaped(body, revealed)


def _marks_escaped(body: str, revealed: str) -> str:
    """The mark half of `_defang`, including its disguise arm.

    Split out for `neutralise_marks`, which must leave envelope tags intact.
    """
    escaped = _MARK_FORGERY.sub("&#91;", body)
    if _MARK_FORGERY.search(revealed):
        escaped = escaped.replace("[", "&#91;")
    return escaped


def neutralise_marks(content: str) -> str:
    """Escape every `SYSTEM_SPEECH_MARK`-shaped span in `content`, leaving envelope tags alone.

    For system text that lands inside an envelope (e.g. a truncation notice in a framed connector
    result): nothing inside an envelope may carry a live mark, since the two would contradict.
    """
    return _marks_escaped(content, content.translate(_INVISIBLE))


# Characters an id may carry: excludes `"`, `<`, `>` so it cannot end the attribute or tag.
_ID_UNSAFE = re.compile(r"[^A-Za-z0-9._:-]")


def defang(content: str) -> str:
    """Neutralise any envelope delimiter in `content` without wrapping it.

    For text shown outside an envelope (an answer under review, an id list), where a forged
    delimiter could still claim to be evidence.
    """
    return _defang(content)


def safe_id(note_id: str) -> str:
    """Reduce `note_id` to the characters an attribute or a label can safely carry.

    Public because the verifier writes note ids into its prompt outside any envelope.
    """
    return _ID_UNSAFE.sub("_", note_id) or "unknown"


def frame_untrusted(content: str, *, note_id: str) -> str:
    """Wrap retrieved `content` from source `note_id` in a data envelope for the model.

    The envelope names the source for citation. Content and id are neutralized so neither can close
    it early; the text is otherwise verbatim. Forgery is closed by defanging, not by the nonce
    alone,
    so no caller may treat a matching delimiter as proof of provenance.
    """
    opening, closing = envelope_delimiters(note_id)
    return f"{opening}{_defang(content)}{closing}"


def envelope_delimiters(note_id: str) -> tuple[str, str]:
    """The opening and closing halves of the envelope for `note_id`, as a pair.

    The envelope's one spelling. Split for callers framing a list of content blocks as one envelope;
    such a caller must still `defang` every span, or a middle span could close the envelope early.
    """
    return f'<{ENVELOPE_TAG} id="{safe_id(note_id)}">\n', f"\n</{ENVELOPE_TAG}>"
