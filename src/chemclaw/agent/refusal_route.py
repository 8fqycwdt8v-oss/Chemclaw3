"""A refusal the model reads must say what *would* be allowed, in one shape every gate shares.

A refusal is returned as the tool's result, so the model decides on it alone: route to something
allowed, report and carry on, or retry the same call. Every refusal therefore carries a footer in
one fixed grammar:

    (refusal | code: plan_not_approved | boundary: … | who can act: … | sanctioned path: …)

- **code** — the stable name of this refusal; finer than `core.turn_signals.RefusalReason`, which
  names the gate and is the wire contract. Not put on the wire.
- **boundary** — which wall was hit, named as a thing rather than a configuration key.
- **who can act** — a kind of party, never a role name or entitlement list, so a refusal is not an
  enumeration oracle.
- **sanctioned path** — the concrete allowed next action, or `NO_PATH`. Honest or absent: a
  fabricated path sends the model round the loop again.

This changes no decision; gates compose the text after deciding. Untrusted names (tool-call names,
model-declared plan tools) are reduced with `framing.safe_id`, whose charset has no space or pipe,
so an interpolated value cannot open a field; `routed` also strips the separator from every value.
`tests/test_refusal_route.py` finds every `routed(code=…)` call by AST and holds the partition.
"""

# The character separating footer fields; field values are prose with commas and semicolons.
_SEPARATOR_CHAR = "|"

# What a pipe inside a value becomes; substituted rather than escaped so nothing needs decoding.
_SEPARATOR_REPLACEMENT = "/"

_SEPARATOR = f" {_SEPARATOR_CHAR} "

# The honest value of `sanctioned path` when there is none; tests assert on this constant.
NO_PATH = "none from here"

# How the footer opens. Public so tests find the footer without copying the spelling.
FOOTER_OPENING = "(refusal"


def routed(
    sentence: str,
    *,
    code: str,
    boundary: str,
    who_can_act: str,
    sanctioned_path: str | None = None,
) -> str:
    """`sentence`, plus the footer telling the model what would be allowed instead.

    Args:
        sentence: The refusal as the chemist reads it, unchanged and first (readers key on the
            `Refused:` prefix).
        code: This refusal's stable machine name (`"plan_not_approved"`, `"dry_run"`, …).
        boundary: The wall that was hit, as a thing rather than as a setting.
        who_can_act: The kind of party that could do this. Never a role name or a list.
        sanctioned_path: The concrete allowed next action; `None` or `""` renders as `NO_PATH`.

    Returns:
        The full refusal text, footer on its own line.
    """
    fields = (
        f"code: {code}",
        f"boundary: {boundary}",
        f"who can act: {who_can_act}",
        f"sanctioned path: {sanctioned_path or NO_PATH}",
    )
    footer = _SEPARATOR.join(
        field.replace(_SEPARATOR_CHAR, _SEPARATOR_REPLACEMENT) for field in fields
    )
    return f"{sentence}\n{FOOTER_OPENING}{_SEPARATOR}{footer})"


def sentence_of(text: str) -> str:
    """`text` without its routing footer — what a reader who is not the model should be shown.

    The footer is addressed to the model; the chemist's transcript (`tool_authz.failure_detail`)
    gets
    the sentence alone. Here because where the footer begins is this module's knowledge. Text with
    no
    footer comes back unchanged.
    """
    return text.split(f"\n{FOOTER_OPENING}")[0]
