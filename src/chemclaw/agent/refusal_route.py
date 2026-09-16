"""A refusal the model reads must say what *would* be allowed, in one shape every gate shares.

**Why this exists.** Each gate in the tool-call chain already refuses in a sentence written for the
chemist, and several of those sentences argue at length for their exact wording against a measured
misreading. None of that is wrong; what it omits is who reads the sentence *first*. A refusal is
returned as the tool's result (`agent/tool_authz._refusal_message`), so the model decides on it
alone between three moves — route to something that is allowed, report the wall and carry on, or
call the identical tool again. Prose that names only the wall supports the last two.

Counted over the eleven sites rather than asserted: five said nothing at all about what would work
instead — every entitlement denial — six named an action of some kind, and exactly **one**,
`plan_gate.out_of_scope_refusal`, named a *tool the model could call right now*. The five others
carried their action as an English clause inside a paragraph of explanation, which is the form a
model paraphrases away when it relays the refusal. The counts are not prose here: the partition
they describe is the fixture `tests/test_refusal_route.py` asserts membership of, so a site added
or removed fails that file rather than falsifying this paragraph.

So every refusal the model reads carries a second line in one fixed grammar:

    (refusal | code: plan_not_approved | boundary: … | who can act: … | sanctioned path: …)

- **code** — the stable machine name for *this* refusal. Deliberately finer than
  `core.turn_signals.RefusalReason`, which names the *gate* and is a wire contract two other
  repositories mirror: three of its four members cover more than one case, with different remedies
  each time, and `authz` alone buckets six — `authorize_tool`'s three, `authorize_trigger`'s two
  and the skills tree's one. A code is not put on the wire and is not a second classification of
  the gate — it is the identity of the sentence, so a model (and a reader of the audit trail's
  `detail`) can tell two refusals apart without matching on prose. `RefusalReason` stays what
  consumers read.
- **boundary** — which wall was hit, named as a thing rather than as a configuration key.
- **who can act** — the party that could do this, when there is one. It names a *kind* of party,
  never a role name, a group or a list of entitlements: a refusal that enumerated what the account
  lacks would answer "which roles exist here" for anyone who can call a tool, which is the same
  enumeration oracle `skill_backend.REFUSED` refuses to be.
- **sanctioned path** — the concrete next action that is allowed, or `NO_PATH`.

**`sanctioned path` is honest or it is absent, and that is the whole discipline.** A fabricated
path is worse than none: it sends the model round the loop again against a wall that has not moved,
which is exactly the behaviour this is written to stop. Five of the eleven refusals genuinely have
no path — an agent cannot grant itself a role or authenticate a request — and they say `none from
here`. The field's value is that "there is nothing you can do about this" is *information*, and
the model had to infer it before. `tests/test_refusal_route.py` holds both halves of that
partition, so inventing a path for an entitlement denial fails rather than ships.

**What it costs, measured rather than waved at.** Across the eleven, the footer adds a mean of
249 characters — about 62 tokens at this tree's chars/4 estimator — against sentences averaging
136. It is paid per *refusal* rather than per model call, so it does not touch the static prefix
`tests/test_context_floor.py` ratchets, and the longest composed refusal stays far inside
`tool_authz._refusal_message`'s bound. A refusal the model relays badly costs a whole extra turn,
which is the comparison that makes 62 tokens cheap; the figure is here because a doubling would
not be, and because a number nobody wrote down is a number nobody re-checks.

**This changes no decision.** Nothing here is consulted by a gate; every one of them composes its
sentence after it has already decided to refuse. Two tests hold that rather than leaving it to
inspection: one drives `authorize_tool` over the whole advertised surface under the posture the
chart ships and requires the refused set to be exactly `DEFAULT_WRITE_TOOL_GATES`, and one covers
the single place the change *could* have moved a decision — a gate keyed on a name `safe_id`
alters, which must still fire on the raw name while printing the reduced one. Mutated to decide on
the printed name, the gate falls **open** and that second test is the only one that catches it.

**The grammar is unforgeable from the values this writes, and the names are reduced at the two
places an unvalidated one enters.** A footer is a grammar, and a grammar is worth forging: a second
`sanctioned path:` field spelled inside an interpolated string would be read by the model as this
system's own routing. Two of the eleven interpolate a string nothing validates — `authorize_tool`
takes whatever name the model put in its tool call, and `out_of_scope_refusal` interpolates the
`tools` a `write_todos` step declared, which is an arbitrary model-authored list
(`plan_scope.step_declaration` keeps every string). Both now pass those names through
`framing.safe_id`, whose charset cannot spell a field, a separator or a delimiter; real tool names
are `[a-z_]+` and come back unchanged. `routed` additionally strips its own separator out of every
value it is handed, so a caller added later cannot open a field by accident. What this does *not*
rest on is the envelope defence: `_refusal_message` defangs the composed text, which neutralises a
smuggled `</retrieved-note-…>` or `[system …]` and says nothing about a smuggled `sanctioned path`.
"""

#: The one character that separates the footer's fields. A pipe rather than a comma or a semicolon
#: because the field *values* are prose written for a reader — they contain commas, semicolons and
#: full stops — and a separator a value can spell is not a separator.
_SEPARATOR_CHAR = "|"

#: What a pipe inside a value becomes. Substituted rather than escaped: an escape sequence is
#: something a reader has to decode, and nothing this system writes contains a pipe, so the
#: substitution is unreachable for every value in the tree today and is here for the caller added
#: next year.
_SEPARATOR_REPLACEMENT = "/"

_SEPARATOR = f" {_SEPARATOR_CHAR} "

#: The honest value of `sanctioned path` when there is none — the case a fabricated path would hide.
#: A constant because five refusals use it and because `tests/test_refusal_route.py` asserts on the
#: distinction rather than on the wording.
NO_PATH = "none from here"

#: How the footer opens. Public so a test can find the footer without copying the spelling, which
#: is the mistake `plan_gate.PLAN_GATE_REASON` records the eval harness making with a refusal
#: phrase: a copy of prose held somewhere else is a reword away from being silently wrong.
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
        sentence: The refusal as the chemist reads it — unchanged, and still the first thing in the
            result, because four readers key on `Refused:` staying the prefix and because a footer
            is a routing aid rather than the explanation.
        code: This refusal's stable machine name (`"plan_not_approved"`, `"dry_run"`, …).
        boundary: The wall that was hit, as a thing rather than as a setting.
        who_can_act: The kind of party that could do this. Never a role name or a list.
        sanctioned_path: The concrete allowed next action, or `None` when there honestly is none.
            `None` renders as `NO_PATH`; an empty string is treated the same way, so a caller that
            computes a path and finds none cannot accidentally emit an empty field.

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

    **The footer is addressed to the model and only to the model**, in the model's second person
    ("answer from what you have", "continue with the tools you do hold"), and two of its four
    fields are machine vocabulary. `agent/tool_authz.failure_detail` feeds the *chemist's*
    transcript (`core.turn_signals.ToolFailureSignal` → `api/events.ToolFailedEvent`) under a
    300-character bound, so without this a chemist read the refusal followed by a footer cut
    mid-word — measured, `plan_scope_excludes_tool` arrived ending `(refusal | code: plan_scope_ex`.

    Here rather than as a `split` at that call site, because where the footer begins is this
    module's knowledge: a literal in `tool_authz` would be a second spelling of the grammar, and
    the footer changing shape would leave the chemist's channel silently wrong. The audit row keeps
    the whole thing (`audit` bounds it separately) — a reviewer querying `detail` wants the code.

    Text with no footer comes back unchanged, so every other failure this is applied to is
    untouched.
    """
    return text.split(f"\n{FOOTER_OPENING}")[0]
