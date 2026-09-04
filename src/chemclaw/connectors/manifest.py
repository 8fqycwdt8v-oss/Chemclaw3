"""The connector manifest: one validated contract for everything a capability contributes.

Why this exists: before connectors, a capability was added in one of four unrelated places — a
`@tool` function in `agent/`, a `settings.mcp_servers` entry, a bespoke Temporal adapter plus a
hand-maintained worker list, and a `SKILL.md` folder — three of which are Python edits to
orchestration code. A capability was a concept the codebase could not name. A `connector.yaml`
names it: one folder declares the tools it serves, the durable jobs it can run, the skills that
teach them, and the agent profiles it enables.

The manifest is *the whole contract*. Everything a connector contributes is declared here and
validated by pydantic with `extra="forbid"`, so a misspelled key fails `make connector-validate`
in CI instead of silently vanishing — the same fail-fast stance `SkillManifest` takes for
`SKILL.md` frontmatter and the config models take for env values.

**What does *not* become a connector, and why the line is here.** A connector holds *capability* —
work whose dependencies and CPU are its own business and whose result is a value. Three kinds of
tool stay in core by rule, and each is a rule rather than a backlog item:

1. **Conversation plumbing** — anything that reads or writes the *turn's* own state (attachments,
   preferences, watches, clarifying questions). Another process does not have the turn.
2. **The PR-gate writers** (`propose_knowledge_note`, `record_confirmed_answer`). The gate is the
   review boundary; a connector reaches it only by returning a `Note` in a job envelope, which is a
   proposal core decides to publish. That asymmetry is the point.
3. **Core's own data layer — the knowledge graph.** This one is worth stating because it looks like
   a capability and is not (D-115). Thirteen core modules import `kg`: the PR-gate, all six memory
   layers, the report retrievers, the eval verifier, the note index. Moving `find_notes`,
   `expand_note` and `find_knowledge_gaps` to a bundle would leave every one of those imports in
   core — a zero dependency win — and add a second read path to one note tree. A capability earns a
   bundle by taking a dependency closure *with* it; the graph cannot, because core is its main
   consumer, not the conversation.

Two shapes vary by kind and are therefore discriminated unions: the transport a connector is
reached over, and how we authenticate to it. They are unions *here*, in the manifest, rather than
in `core/config/` — which is the whole point, and is now the rule rather than this
file's preference: config says which attached things exist and where, a manifest says what
each one is
(D-118, D-120). The two config-side unions this docstring used to cite as precedent,
`McpServerSpec` and `DataSourceSpec`, were both replaced by manifests for that reason.

Adding a transport or an auth mode is one variant plus one branch at the single dispatch site,
never a widening of one model with optional fields that only apply sometimes.
"""

import re
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from chemclaw.core.http import is_loopback_url

# A job parameter's declared type, mapped to a Python annotation by `connectors.jobs`.
# Deliberately a *closed* set: the generated pydantic model becomes the JSON schema the model
# fills in, and a schema the model can always fill correctly is worth more than an open type
# language. These cover every launch argument the existing durable jobs take (SMILES strings,
# method names, counts, flags, lists of either, and one nested spec object).
JobParamType = Literal["string", "integer", "number", "boolean", "string[]", "number[]", "object"]


class NoAuth(BaseModel):
    """No credential — the connector is inside our own trust boundary.

    Correct for a stdio connector (a subprocess of our own pod, under our own identity) and for
    a loopback HTTP connector in dev. `HttpEndpoint` refuses it for a non-loopback declared URL,
    reusing the front door's loopback rule (`chemclaw.core.http.LOOPBACK_HOSTS`) rather than
    inventing a second notion of "safe address".

    **This paragraph used to describe a validator that did not exist** — the rule lived in this
    docstring and nowhere else in the tree, so a manifest could ship pointing at a network host with
    no credential and nothing would say so. It cost nothing while every bundle was ours and shipped
    a loopback default, and stopped being free the moment a bundle could name somebody else's server
    (D-2026-08-09-a-connector-we-do-not-run). The rule is now on `HttpEndpoint`, which is where the
    URL and the auth mode are both in scope.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["none"] = "none"


class BearerAuth(BaseModel):
    """A bearer token read from the environment at call time (the in-cluster three-secret model).

    `token_env` names the variable rather than carrying the token, so no credential is ever
    written to a manifest in the repo. It is read per request, not at import, so a rotated
    secret is picked up without a restart — and a missing variable fails the call with a clear
    error instead of silently sending an empty `Authorization` header.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["bearer"] = "bearer"
    token_env: str = Field(min_length=1)


# How chemclaw authenticates to a connector, discriminated on `mode`. Only the two modes with a
# real caller are built: `none` for the trust-boundary cases and `bearer` for everything
# in-cluster. The Entra service-identity and on-behalf-of modes are a documented extension
# point, not a stub — OBO needs the user's raw access token, which `service.auth.Principal`
# deliberately does not carry, and neither exchange can be verified without a real tenant (see
# `docs/archive/plans/connector-plan.md` §11).
ConnectorAuth = NoAuth | BearerAuth


class HttpEndpoint(BaseModel):
    """A connector reached over MCP streamable-HTTP — the normal case (its own FastAPI server).

    `health_url` is optional and only used by the startup probe: a connector we wrote exposes
    `/healthz`, while a third-party MCP server may expose nothing, and reporting such a
    connector as "unprobed" is honest where guessing a path would produce a false alarm.
    `request_timeout` (whole seconds) is how long one tool call may take before it is abandoned —
    the bound that keeps a mute or slow connector from hanging a turn. `None` does **not** defer to
    the MCP client, which has no default of its own: it reaches `anyio.fail_after(None)` and waits
    forever, so `None` means "take the registry's `_DEFAULT_REQUEST_TIMEOUT_SECONDS`" rather than
    "unbounded". `chemclaw.connectors.registry.request_timeout_seconds` is where that is decided.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    transport: Literal["http"] = "http"
    url: str = Field(min_length=1)
    health_url: str | None = None
    request_timeout: int | None = Field(default=None, gt=0)
    auth: ConnectorAuth = Field(default_factory=NoAuth, discriminator="mode")
    tools: list[str] = Field(default_factory=list)
    state_changing: list[str] = Field(default_factory=list)
    read_only: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _every_tool_is_classified(self) -> Self:
        """Reject an endpoint that does not classify each of its tools exactly once."""
        _check_classification(self.tools, self.state_changing, self.read_only)
        return self

    @model_validator(mode="after")
    def _a_networked_endpoint_carries_a_credential(self) -> Self:
        """Reject `auth: mode: none` on a URL that is reachable from the network.

        The rule is about the *declared* URL — what a bundle ships in the repo — and not about the
        effective one after `connector_urls`, which is a deliberate line rather than an oversight.
        A deployment override points at the operator's own infrastructure (in the shipped chart, an
        in-cluster Service bounded by the `connector-ingress` NetworkPolicy), and a validator that
        failed on those would flag the entire shipped fleet the moment the chart set the override —
        an alarm that fires on the normal case teaches people to disable it. What it does catch is
        the case that has no compensating control and is now expressible: a manifest naming somebody
        else's host, reached across a network we do not own, with no credential on the call.

        `NoAuth` stays the default because the transports it is right for — stdio, and the loopback
        dev endpoint every shipped bundle declares — are the common ones; this makes the default
        unavailable exactly where it stops being true.
        """
        if isinstance(self.auth, NoAuth) and not is_loopback_url(self.url):
            raise ValueError(
                f"endpoint url {self.url!r} is not loopback, so `auth: mode: none` would send "
                "every call across the network with no credential; declare "
                "`auth: {mode: bearer, token_env: ...}`, or use a loopback URL and let the "
                "deployment move it with CHEMCLAW_CONNECTOR_URLS"
            )
        return self


class StdioEndpoint(BaseModel):
    """A connector launched as a subprocess of the agent's own process (dev, and pure-local tools).

    The same agent-facing surface as `HttpEndpoint` — callers never branch on the transport —
    but no identity headers travel: there is no request to attach them to, and the subprocess
    already runs under our identity. Kept because it is the zero-infrastructure path for a local
    capability and
    for tests, not as the recommended production shape.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    transport: Literal["stdio"] = "stdio"
    command: str = Field(min_length=1)
    args: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    state_changing: list[str] = Field(default_factory=list)
    read_only: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _every_tool_is_classified(self) -> Self:
        """Reject an endpoint that does not classify each of its tools exactly once."""
        _check_classification(self.tools, self.state_changing, self.read_only)
        return self


def _check_classification(
    tools: list[str], state_changing: list[str], read_only: list[str]
) -> None:
    """Raise unless every served tool is in exactly one of `state_changing` and `read_only`.

    **A partition, not two optional hints, and the strictness is the whole point.** Whether a tool
    spends real resources or merely looks something up decides whether the harness's plan gate
    refuses it under an unapproved plan (D-167), and every way of getting that wrong fails *open* —
    a typo matches nothing, an omission reads as "harmless", and either ships a write that looks
    exactly like a gated one. Defaulting an undeclared tool to "read" would put the whole burden on
    a bundle author remembering; defaulting it to "write" would gate a connector's lookups and make
    the approval-first posture unusable. Refusing to load is the only option that cannot be wrong
    quietly, and it costs a bundle author one line per tool, once.

    Core still cannot infer the answer — that is exactly why the bundle has to state it.

    **An empty `tools` list is refused for the same reason, and it is not the same check.** A
    partition of nothing is trivially satisfied, so an endpoint that simply omits `tools:` passed
    this function while turning both of its guarantees off at once: `registry` read the empty list
    as "no allow-list" and bound the server's entire advertised surface, and none of what arrived
    appeared in `state_changing_tool_names()`, so `agent.authz.side_effecting_call` answered `False`
    for every one of them — including a write. That is the plan gate's input (D-167) and the
    dry-run gate's, so the manifest that declared the least got the most.
    """
    served = set(tools)
    if not served:
        raise ValueError(
            "endpoint declares no tools; an endpoint that serves nothing cannot be reached, and "
            "an empty list makes this partition vacuous — every tool the server advertises would "
            "arrive unclassified and be treated as a read"
        )
    classified = set(state_changing) | set(read_only)
    unknown = sorted(classified - served)
    if unknown:
        raise ValueError(
            f"endpoint classifies tool(s) {unknown} it does not serve; tools: {sorted(served)}"
        )
    unclassified = sorted(served - classified)
    if unclassified:
        raise ValueError(
            f"endpoint does not say whether tool(s) {unclassified} change state; list each under "
            "`state_changing` (it spends resources or writes data) or `read_only` (it looks "
            "something up)"
        )
    both = sorted(set(state_changing) & set(read_only))
    if both:
        raise ValueError(f"endpoint lists tool(s) {both} as both state_changing and read_only")


# One connector endpoint, discriminated on `transport`. A new transport is one variant here plus
# one branch in `connectors.registry._mcp_connection`. Both variants carry `tools` — the
# agent-facing allow-list — because it is a property of *an endpoint's* surface: nesting it
# here rather than at the manifest's top level makes "an allow-list with no endpoint to serve
# it" unrepresentable instead of something a validator has to catch.
#
# `state_changing` names the subset of `tools` that spends real resources or writes data a person
# would care about — the ones the harness's plan gate refuses under an unapproved plan (D-167).
# It is declared **here, by the bundle**, and not as a list in core, for the same reason the queue
# and the params model are: whether `predict_pka` is a lookup or a calculation is the capability's
# own fact, and a copy of it in core is a second source of truth that goes stale the first time a
# bundle changes what a tool does. There is no such thing as an undeclared tool: `tools` may not be
# empty and every entry must be classified, because both ways of leaving it blank end at the same
# place — a write the plan gate reads as a read.
Endpoint = HttpEndpoint | StdioEndpoint


class JobParam(BaseModel):
    """One launch argument of a durable job, as the model will see it.

    `description` is required, not optional: it becomes the argument's schema description, which
    is the only thing telling the model what to put there. A job whose parameters are
    undocumented is a job the model will call wrongly, so the manifest refuses to declare one.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    type: JobParamType
    description: str = Field(min_length=1)
    required: bool = True


class EffectSpec(BaseModel):
    """What a job changes in a system this deployment does not own, and whether it can be undone.

    **The write path was never missing; the distinction was.** `ConnectorManifest` already routes
    mutation through `jobs:` — "which core authorizes, dry-run-gates and attributes" — and has since
    D-029. What nothing said was whether a job writes *our* database or somebody else's system of
    record, and the two are not the same act: re-running a cached calculation is free, and filing a
    deviation twice is a second deviation. Nothing declared reversibility either, so every job was
    gated identically whether it could be undone or not.

    That gap is what `D-2026-08-15-the-plan-gate-stays-a-refusal-because-an-interrupt-cannot-ask-
    the-question` left open in as many words: `HumanInTheLoopMiddleware` was declined for plan
    approval and **"not declined for per-call approval of an irreversible action, which is a
    different, still-open question."** This is the declaration that makes that question askable.

    Declaring this changes three things at once, and each is enforced rather than requested:

    - the job is **state-changing** and **expensive**, so the plan gate and `authorize_trigger` both
      see it (`_effects_are_gated` below refuses a manifest that says otherwise);
    - `reversal: irreversible` means the run **suspends on a human approval before it acts**, which
      is what the durable wait exists for and is a per-call decision rather than a plan-wide one;
    - every attempt is recorded in `effects` — before and after — so an evidence pack can say what
      this system changed outside itself, and so a crashed run leaves a row saying it *might* have.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    system: str = Field(
        min_length=1,
        description=(
            "What this reaches, in the operator's words — 'the QMS', 'the LIMS', 'the portfolio "
            "tool'. Free text and never parsed: it is what a person reads in an approval request "
            "and in an evidence pack, and a vocabulary this repository invented would be a list of "
            "the systems it happened to imagine."
        ),
    )
    reversal: Literal["idempotent", "compensating", "irreversible"] = Field(
        description=(
            "Whether the effect can be undone. `idempotent` — applying it twice is applying it "
            "once (setting a status, upserting a row). `compensating` — it can be undone by "
            "another declared job, named in `compensation`. `irreversible` — it cannot, so a human "
            "approves this specific call before it runs. **There is no default**, because the "
            "safe-looking one is the wrong one: a job whose author did not think about reversal is "
            "far likelier to be irreversible than idempotent, and a default would let the "
            "un-thought-about case take the cheapest gate."
        )
    )
    compensation: str = Field(
        default="",
        description=(
            "The job that undoes this one, for `reversal: compensating`. A job name in this same "
            "bundle — not a workflow type, so it is gated, attributed and recorded exactly as any "
            "other effect, including being an effect itself."
        ),
    )

    @model_validator(mode="after")
    def _compensating_names_its_compensation(self) -> Self:
        """A compensating effect must name what undoes it, and no other kind may name one.

        Both directions, because both are a claim: an unnamed compensation is a reversibility
        nobody can perform, and a compensation on an irreversible effect is the opposite claim in
        the same field.
        """
        if self.reversal == "compensating" and not self.compensation:
            raise ValueError(
                "an effect declaring `reversal: compensating` must name the job that undoes it "
                "in `compensation:` — a reversibility nobody can perform is not one"
            )
        if self.reversal != "compensating" and self.compensation:
            raise ValueError(
                f"an effect declaring `reversal: {self.reversal}` also names a `compensation:`; "
                "only a compensating effect has one"
            )
        return self


class JobSpec(BaseModel):
    """A durable capability: one generated agent tool that starts one connector-owned workflow.

    The connector owns the workflow *code* and the worker that serves it; this spec is how core
    reaches it without importing it. `workflow` is a Temporal workflow **type name** — a string,
    so core has no build-time dependency on the connector at all.

    **The queue is not declared here.** It is `bundle_queue(connector)`, derived at dispatch, for
    the reason D-150 gives: a bundle's worker serves what the bundle's own modules registered at
    import time, so `connector-<name>` is the only queue on which this workflow type exists. A
    declared queue could therefore hold exactly one correct value and any number of wrong ones,
    each of which starts a job successfully and then leaves it in a queue nobody polls.

    `expensive` puts the job in the coarse `authorize_trigger` gate's set (a costly search or BO
    run must
    be entitled, not merely authenticated) — the declaration *is* the gate's source, read by
    `chemclaw.agent.authz.expensive_actions`, so it needs no matching operator entry and gains
    nothing from one. It was for a while a marker that authorized nothing, because the gate
    consulted only `entra_expensive_actions`; `tests/test_authz.py` now cross-checks every declared
    job against the effective set. `publish_to_graph` lets core PR-gate a `Note` the job's result
    carries — the write still goes through `chemclaw.kg.pr_gate`, never through the connector.

    **A bundle may lower its own runtime ceiling and may not raise it** (`timeout_seconds`). The
    deployment keeps the maximum — the effective ceiling is the *lower* of the declared number and
    `connector_job_timeout_seconds` — so a manifest that asks for more than the operator funds is
    clamped rather than obeyed, and a manifest that declares nothing is bounded exactly as it was
    before this field existed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    # The advertised tool name, so it is keyed and gated exactly like a hand-written tool
    # (`tool_role_gates`, `DEFAULT_WRITE_TOOL_GATES`, profile narrowing all address this
    # string).
    name: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    workflow: str = Field(min_length=1)
    # The first line of the generated tool's docstring — what the model reads when deciding to
    # call it. `description` carries the rest (when to use it, what the id is for, idempotency).
    summary: str = Field(min_length=1)
    description: str = ""
    # A job declares its launch arguments one of two ways, and the choice is about *fidelity*,
    # not taste. `params` is the easy path: flat, closed-type arguments declared inline, which
    # covers a job whose input is a handful of scalars (a SMILES, a method name, a count).
    # `params_model` is the full-fidelity path: a dotted `module:Attribute` reference to an
    # existing pydantic model, for a job whose input is a rich domain object — a nested
    # optimization problem with discriminated feature kinds cannot be re-declared in YAML
    # without losing exactly the structure that makes the model call it correctly, and
    # re-declaring it would be a second source of truth for a schema that already exists in
    # code. Declaring neither is a job with no arguments.
    params: list[JobParam] = Field(default_factory=list)
    params_model: str | None = Field(default=None, pattern=r"^[\w.]+:[A-Za-z_]\w*$")
    # A domain precondition checked *before* any durable work starts: a dotted `module:function`
    # taking the validated params object and raising to refuse the launch.
    #
    # It exists because the checks a durable capability needs at creation time are not all
    # generic. Authorization and dry-run are (`expensive`, and the ambient flag), but "this
    # campaign asks for more rounds than Temporal's event history can hold" is the BO domain's
    # own rule — and every other place to put it is replay-unsafe. A pydantic validator on the
    # params model, or a check inside the workflow, re-runs during replay against *current*
    # config, so lowering the ceiling would retroactively fail an in-flight campaign that was
    # legal when it started. Only the launch boundary is safe, and after the generic factory
    # replaced the hand-written adapters this is the launch boundary. Declaring it is how a job
    # keeps a guard it would otherwise silently lose.
    precondition: str | None = Field(default=None, pattern=r"^[\w.]+:[A-Za-z_]\w*$")
    expensive: bool = False
    publish_to_graph: bool = False
    # What this job changes *outside* this deployment, and whether it can be undone. Absent means
    # the job's writes are this system's own — a calculation cached, a note proposed, a row
    # recorded — which is every job in this repository today.
    effect: EffectSpec | None = None
    # How long the launcher waits for the run to finish before handing back a job id instead.
    # Unset (the default) means "always a job": start it, return the id, poll it.
    #
    # This exists for the capability whose cost varies by orders of magnitude with its input. A
    # reaction energy over two small species is a couple of seconds and belongs *in* the answer;
    # the same tool over eight species with Hessians is minutes and must not hold a conversation
    # open. Declaring one number here lets one tool serve both, and the model sees a result or a
    # job id without having to choose between two tools on a cost estimate it cannot make.
    #
    # Deliberately a wait on the real run rather than a predicted-cost threshold, which is what
    # this replaced: a prediction is a second model of the calculation that can be wrong in both
    # directions (a slow "cheap" call blocks the turn anyway; a fast "expensive" one is deferred
    # for nothing), and it can only live where the cost model lives — which would put chemistry
    # back in core, the exact coupling the seam removes. Elapsed time needs no model and is
    # always right.
    #
    # Keep it comfortably under the front door's `service_turn_timeout_seconds`: this budget is
    # spent inside a turn, and a job that outlives the turn is the failure it exists to prevent.
    inline_wait_seconds: float | None = Field(default=None, gt=0)
    # A ceiling on this job's whole durable run, in seconds — **a lowering of the deployment's
    # ceiling, never a raise.** The effective ceiling is
    # `min(this, connector_job_timeout_seconds)`, computed in one place
    # (`durable/connector_job.py::child_execution_timeout`), so a manifest in this repository can
    # ask for *less* runtime than the deployment funds and never for more. That asymmetry is the
    # whole reason this field can exist at all: `connector_job_timeout_seconds` is one global
    # number precisely because a bundle must not be able to grant itself unlimited runtime, and a
    # bound that can only move downward takes nothing away from the operator.
    #
    # Unset (the default) means exactly the deployment's ceiling — what every manifest written
    # before this field existed got, and still gets.
    #
    # It exists because one global ceiling bounds a twenty-second job and a four-hour job
    # identically. With a bundle's worker down, a job that would have answered in seconds sits
    # `running` for the whole global ceiling with nothing said, because the only thing that ends it
    # is a number sized for the *longest* job in the fleet. The bundle knows what its own job
    # costs; the deployment knows the maximum it will fund. Declaring the first here keeps both.
    #
    # **Declare what this job actually costs, and never less than the longest activity its own
    # workflow runs.** Core cannot check that half and does not pretend to: it can see neither the
    # bundle's workflow nor its activity budgets, so a ceiling below the child's own activity
    # budget re-creates — for this one job — the defect
    # `Settings._the_job_ceiling_covers_the_activity_it_bounds` refuses globally: a single attempt
    # exhausts the whole ceiling, the activity's retry policy becomes unreachable, and the run dies
    # as a bare `WorkflowExecutionTimedOut` naming no setting at all.
    timeout_seconds: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _effects_are_gated(self) -> Self:
        """A job that changes somebody else's system is expensive by declaration, not by choice.

        `expensive` is what puts a job in `authorize_trigger`'s set, so a manifest could otherwise
        declare an external effect that any authenticated user could trigger. Refused rather than
        silently corrected: a manifest saying `expensive: false` beside an `effect:` block is an
        author who believed one of the two, and which one they believed matters.
        """
        if self.effect is not None and not self.expensive:
            raise ValueError(
                f"job {self.name!r} declares an `effect:` on {self.effect.system!r} but is not "
                "`expensive: true`. A job that changes a system this deployment does not own must "
                "be entitled, not merely authenticated"
            )
        return self

    @model_validator(mode="after")
    def _one_way_to_declare_params(self) -> Self:
        """Reject a job declaring params both inline *and* by model — which wins is a coin flip."""
        if self.params and self.params_model is not None:
            raise ValueError(
                f"job {self.name!r} declares both `params` and `params_model`; use one "
                "(inline for flat scalar arguments, a model reference for a structured input)"
            )
        return self

    @model_validator(mode="after")
    def _distinct_param_names(self) -> Self:
        """Reject two parameters sharing a name — one would shadow the other in the model."""
        names = [param.name for param in self.params]
        duplicated = sorted({name for name in names if names.count(name) > 1})
        if duplicated:
            raise ValueError(f"job {self.name!r} declares duplicate parameter(s) {duplicated}")
        return self


class ConnectorManifest(BaseModel):
    """One `connectors/<name>/connector.yaml`: everything that capability contributes.

    A connector must contribute *something reachable* — an endpoint, a job, or both — for the
    same reason `chemclaw.ingest.sources.base.SourceSpec` rejects a source with neither half: a
    connector that
    serves no tools and runs no jobs is not a connector. A bundle that only ships skills or
    profiles belongs in the skills tree, not here.

    The endpoint's `tools` allow-list is **read/compute only** by contract. Mutation goes
    through a `jobs:` entry (which core authorizes, dry-run-gates and attributes) or stays a
    core PR-gate tool. That is the existing `allowed_tools` boundary (D-029) promoted from a
    convention to a validated contract: `make connector-validate` refuses a name matching the
    mutating-tool prefixes, so a connector cannot quietly hand the model a write path.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9-]*$")
    description: str = Field(min_length=1)
    endpoint: Endpoint | None = Field(default=None, discriminator="transport")
    jobs: list[JobSpec] = Field(default_factory=list)
    # Names of the `SKILL.md` folders under this bundle's `skills/` dir and the profile files
    # under its `profiles/` dir. Declared rather than inferred so a stray folder is a CI failure
    # instead of a silently-shipped skill (`scripts.validate_connectors`).
    skills: list[str] = Field(default_factory=list)
    profiles: list[str] = Field(default_factory=list)
    # The knowledge-graph vocabulary this bundle's `publish_to_graph` jobs mint, unioned into
    # `KNOWN_NOTE_TYPES`/`KNOWN_RELATIONS` by `chemclaw.kg.note.known_note_types` and its sibling.
    #
    # **Why a bundle may extend a closed vocabulary.** Those two frozensets are closed on purpose:
    # a typo makes a note or an edge unfindable by every filter keyed on it, so the vocabulary is
    # checked at the PR-gate rather than left open. But the vocabulary is not core's alone —
    # `bo-candidate` is minted by a bundle (`connectors/bo/knowledge.py`) and was written into
    # core's frozenset by hand. That made a bundle contributing a note type the one connector
    # contribution needing a core edit, in the seam whose whole claim is that a capability is a
    # folder (D-118).
    #
    # Declaring it here keeps both properties: the set is still closed (an undeclared name still
    # fails `make kg-validate`), a human still sees a genuinely new type at the gate that reviews
    # the bundle, and the deployment's effective vocabulary is exactly what its enabled bundles say
    # it is. Names are validated for shape here and for *existence* nowhere — a type nothing has
    # minted yet is a declaration, not an error.
    note_types: list[str] = Field(default_factory=list)
    relations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _vocabulary_is_well_formed(self) -> Self:
        """Reject a note type or relation that is not a lowercase hyphenated token.

        The same shape the shipped vocabulary uses (`bo-candidate`, `computed-from`). Enforced
        because these names become path segments (`knowledge/<type>/<id>.md`) and frontmatter keys:
        a name with a slash, a space or an uppercase letter would produce a note that validates and
        then cannot be found by the filters keyed on it — the exact failure the closed vocabulary
        exists to prevent, arriving through the door opened for extending it.
        """
        for field, values in (("note_types", self.note_types), ("relations", self.relations)):
            bad = sorted(v for v in values if not re.fullmatch(r"[a-z][a-z0-9-]*", v))
            if bad:
                raise ValueError(
                    f"connector {self.name!r}: {field} entries must be lowercase hyphenated "
                    f"tokens (e.g. 'job-result'); got {bad}"
                )
        return self

    @model_validator(mode="after")
    def _contributes_capability(self) -> Self:
        """Reject a manifest with neither an endpoint nor a job — nothing could ever reach it."""
        if self.endpoint is None and not self.jobs:
            raise ValueError(
                f"connector {self.name!r} must declare an endpoint, a job, or both "
                "(a bundle with neither serves no capability)"
            )
        return self

    @model_validator(mode="after")
    def _distinct_job_names(self) -> Self:
        """Reject two jobs sharing a tool name — the second registration fails at build time."""
        names = [job.name for job in self.jobs]
        duplicated = sorted({name for name in names if names.count(name) > 1})
        if duplicated:
            raise ValueError(f"connector {self.name!r} declares duplicate job name(s) {duplicated}")
        return self
