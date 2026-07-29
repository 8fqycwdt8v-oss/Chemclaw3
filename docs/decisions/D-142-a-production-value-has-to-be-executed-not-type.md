# D-142 — A production value has to be executed, not type-checked — and two guards that were off in the one deployment that needed them

**Status:** accepted · **Context:** REV-15 and REV-16. One is about what the chart tests can prove;
the other is about what the chart actually ships. They belong together because the first is what
makes the second checkable.

### The parity check did not reach half the pod's environment (REV-15)

`tests/test_helm_chart.py` built its view of pod environment from `.Values.config`, the secret refs,
the mTLS paths and `_helpers.tpl`. It never read `templates/config.yaml`, which *derives* two more
keys rather than copying them: `CHEMCLAW_NOTE_REPO_DIR` from the knowledge volume layout, and
`CHEMCLAW_CONNECTOR_URLS` from the enabled bundle set. Both were outside **both** tests — neither
"is this a real setting" nor "does this value load" applied to them.

`connector_urls` is the one that matters: a `dict[str, str]` parsed from rendered JSON, which is
exactly the shape that constructs fine in a unit test and crashes every pod at import when the
render is wrong. It had never been fed a rendered value at all.

**Decision:** discover the derived keys from the template (so a third is covered on the day it is
added), reproduce the helper's render offline, and feed both through `Settings`.

Writing that surfaced the more interesting half. Passing the rendered JSON as an `__init__` kwarg
**fails** with `dict_type`: pydantic-settings JSON-decodes a complex field from an environment
variable and does not from a kwarg. So the existing test's model of "the pod environment" was not
merely incomplete for these keys, it was the wrong mechanism — and a test that constructs `Settings`
from literals cannot discover that. The derived keys now go through `monkeypatch.setenv`, which is
how the pod receives them.

The `connector_urls` result is asserted, not merely constructed: a render that produced `{}` still
builds a perfectly valid `Settings` while pointing the front door at nothing. Verified by disabling
every connector server in `values.yaml` and watching the test go red.

**And the inverse direction now has tests of its own.** This is the lesson D-136 paid for: OTel was
enabled in the chart, loaded as a perfectly valid bool, and CrashLoopBackOff'd every Python
component on first deploy because the SDK was not in the dependency closure. `test_logging.py` added
one executed-value test for that case; the two below generalize the shape — take the shipped values
and assert the thing they switch on actually *happens*.

### Two guards that were off in the only deployment that needed them (REV-16)

**`budget_enabled` → true in the chart.** Its rationale for being off was, in full, "Off by default
(today's behavior)" — off because it was off. Off is right as a *code* default: a CLI or a test must
not answer 429. But a deployment serving real users has no reason to be unguarded. A single turn is
iteration-capped and the *number* of turns is not, so a client or an automated push-back loop
accumulates unbounded LLM spend, and the load run that validated this system ran with budgets on —
so "on" is the configuration that was actually measured.

**`audit_verify_enabled` → true in the chart.** Its docstring says it "only earns a Schedule where a
durable audit sink is actually configured". This chart sets `SESSION_STORE: postgres`, which is
precisely what makes `default_audit_sink()` durable — so the precondition holds here and nowhere
else, and the flag was still off. The one deployment that *has* a tamper-evident chain was the one
never checking it, and a chain nobody checks detects tampering only after somebody thinks to look,
which is the failure mode the chain exists to remove.

**`connectors_required` deliberately left false.** The third flag REV-16 named, and the one the
review had wrong. Unlike the other two, its docstring is a real considered trade — `false` is
"degrade loudly", `true` is fail-fast "for a deployment where serving with a silently reduced tool
surface is worse than not serving at all". The review's argument for flipping it was that the
degradation was silent. **That was true when the review was written and is no longer true**: D-139
made an unreachable connector produce a `CapabilityDegradedEvent`, a WARNING and a counter. Flipping
to fail-fast now would trade availability away for a property already obtained more cheaply — one
dark connector taking down the whole front door, to get visibility that already exists. Recorded
rather than done, because a switch whose reasoning has been read is the only kind worth flipping.

Both flags that did change are pinned by *executed* tests rather than by asserting the flag: the
budget one drives a `BudgetTracker` past a cap under the chart's own settings (because
`budget_enabled=true` with every cap at 0 also parses and guards nothing), and the audit one asserts
`audit-verify` appears in the built schedule list (because a flag is one branch away from a schedule
that is planned and never applied). Both go red when the value is set back to `"false"`.
