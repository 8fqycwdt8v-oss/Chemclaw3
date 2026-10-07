"""The connector manifest: one validated contract for everything a capability contributes.

A `connector.yaml` names a capability: the tools it serves, the durable jobs it runs, the skills
that teach them and the profiles it enables. Validated with `extra="forbid"`, so a misspelled key
fails `make connector-validate` instead of silently vanishing.

Three kinds of tool stay in core by rule: conversation plumbing (another process does not have the
turn), the graph writers (one write path stamps provenance; a connector reaches the graph only by
returning a `Note` in a job envelope), and the knowledge graph's own reads (core is its main
consumer, so a bundle would take no dependency closure with it).

Transport and auth vary by kind and are discriminated unions here, in the manifest, not in
`core/config/`: config says which attached things exist and where, a manifest says what each one
is (D-118). Adding a variant is one model plus one branch at its single dispatch site.
"""

import re
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from chemclaw.core.http import is_loopback_url
from chemclaw.core.manifest_io import MAX_MANIFEST_TEXT_CHARS

# A job parameter's declared type, mapped to an annotation by `connectors.jobs`. Closed on purpose:
# the generated model becomes the JSON schema the model fills, and every type here is one it can
# fill correctly.
JobParamType = Literal["string", "integer", "number", "boolean", "string[]", "number[]", "object"]


class NoAuth(BaseModel):
    """No credential — the connector is inside our own trust boundary.

    Correct for a stdio connector (a subprocess of our own pod, under our own identity) and for
    a loopback HTTP connector in dev. `HttpEndpoint` refuses it for a non-loopback declared URL,
    reusing the front door's loopback rule (`chemclaw.core.http.is_loopback_host`) rather than
    inventing a second notion of "safe address" — which is now literally one function rather than a
    shared name: that rule was a set of three literal strings until 2026-09-05, when it was measured
    against `core.netguard`'s parsed copy and found to disagree on `127.0.0.2` and `0.0.0.0`. A
    manifest whose URL names a second loopback address therefore used to be refused here as though
    it were a network host.

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


# How chemclaw authenticates to a connector, discriminated on `mode`: `none` for trust-boundary
# cases, `bearer` for everything in-cluster. Only modes with a real caller are built.
ConnectorAuth = NoAuth | BearerAuth


class QueuedDispatch(BaseModel):
    """Which of an endpoint's tools reach their server through a durable queue, not a direct call.

    A tool named here is still bound, authorized, audited and plan-gated exactly as any other — the
    agent cannot tell the difference and neither can the middleware chain. What changes is the last
    hop: instead of calling the server on the turn's own session, the call becomes a
    `QueuedToolWorkflow` on this connector's interactive queue
    (`chemclaw.connectors.queues.interactive_queue`), and a worker sized to the server's slots takes
    it when one is free. So a burst waits in one global, first-come queue rather than being refused
    pod by pod, the turn waits `inline_wait_seconds` for the answer, and a call that outlasts that
    returns a job id and delivers its result through the session mailbox like any durable job
    (`D-2026-09-30-a-heavy-tool-call-waits-in-a-queue-rather-than-being-refused`).

    **Only a tool whose answer is a function of its arguments belongs here.** Identical concurrent
    calls rejoin one run — the cross-process single-flight `cached_compute` lacks — which is right
    for a calculation and wrong for anything that reads or writes per-caller state.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tools: list[str] = Field(min_length=1)
    inline_wait_seconds: float = Field(gt=0)


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
    knowledge_read: list[str] = Field(default_factory=list)
    queued: QueuedDispatch | None = None

    @model_validator(mode="after")
    def _every_tool_is_classified(self) -> Self:
        """Reject an endpoint that does not classify each of its tools exactly once."""
        _check_classification(self.tools, self.state_changing, self.read_only)
        _check_knowledge_reads(self.knowledge_read, self.read_only)
        return self

    @model_validator(mode="after")
    def _queues_only_tools_it_serves(self) -> Self:
        """Reject a queued tool the endpoint does not serve.

        Such a name would queue nothing while looking as if it did. Not tied to the read/write
        split: cost is a different axis from side effects.
        """
        if self.queued is None:
            return self
        names = self.queued.tools
        if len(names) != len(set(names)):
            raise ValueError(f"`queued.tools` lists a tool more than once: {sorted(names)}")
        unserved = sorted(set(names) - set(self.tools))
        if unserved:
            raise ValueError(
                f"`queued.tools` names tool(s) {unserved} this endpoint does not serve"
            )
        return self

    @model_validator(mode="after")
    def _a_networked_endpoint_carries_a_credential(self) -> Self:
        """Reject `auth: mode: none` on a URL that is reachable from the network.

        Judges the declared URL, not the `connector_urls` override, which points at the operator's
        own infrastructure and would otherwise flag every in-cluster deployment. What it catches is
        a manifest naming a foreign host with no credential. `NoAuth` stays the default for stdio
        and loopback.
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
    knowledge_read: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _every_tool_is_classified(self) -> Self:
        """Reject an endpoint that does not classify each of its tools exactly once."""
        _check_classification(self.tools, self.state_changing, self.read_only)
        _check_knowledge_reads(self.knowledge_read, self.read_only)
        return self


def _check_classification(
    tools: list[str], state_changing: list[str], read_only: list[str]
) -> None:
    """Raise unless every served tool is in exactly one of `state_changing` and `read_only`.

    The classification decides whether the plan gate refuses a tool under an unapproved plan, and
    every way of getting it wrong (a typo, an omission, a default) fails open. So it is a strict
    partition, and an empty `tools` list is refused too: it would bind the server's whole surface
    with nothing classified as state-changing.
    """
    served = set(tools)
    # Checked before the partition, whose set comparisons would hide a duplicated name until a
    # confusing collision error one layer up.
    if len(tools) != len(served):
        repeated = sorted({name for name in served if tools.count(name) > 1})
        raise ValueError(
            f"endpoint lists tool(s) {repeated} more than once; a tool is declared once and "
            "classified once"
        )
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


def _check_knowledge_reads(knowledge_read: list[str], read_only: list[str]) -> None:
    """Raise unless every declared knowledge read is one of this endpoint's own reads.

    Optional, unlike the classification: an omission only understates `turn_costs.retrieval_calls`
    rather than removing a control. What it refuses is a search over the record that the same
    endpoint calls state-changing.
    """
    unknown = sorted(set(knowledge_read) - set(read_only))
    if unknown:
        raise ValueError(
            f"endpoint lists tool(s) {unknown} as knowledge_read but not as read_only; a search "
            "over the record is a read, and a tool that writes may not be counted as one"
        )


# One connector endpoint, discriminated on `transport`; a new transport is one variant here plus one
# branch in `connectors.registry._mcp_connection`. `tools` (the agent-facing allow-list) lives on
# the endpoint, so an allow-list with nothing to serve it is unrepresentable.
#
# `state_changing` names the tools the plan gate refuses under an unapproved plan; `knowledge_read`
# names the subset of `read_only` that consults the record. Both are declared by the bundle, because
# what a tool does is the capability's own fact, and a copy in core would go stale.
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
    description: str = Field(min_length=1, max_length=MAX_MANIFEST_TEXT_CHARS)
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

        Both directions are claims: an unnamed compensation cannot be performed, and a compensation
        on an irreversible effect contradicts it.
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
    job against the effective set. `publish_to_graph` lets core record a `Note` the job's result
    carries — the write goes through `chemclaw.kg.record`, never through the connector.

    **A bundle may lower its own runtime ceiling and may not raise it** (`timeout_seconds`). The
    deployment keeps the maximum — the effective ceiling is the *lower* of the declared number and
    `connector_job_timeout_seconds` — so a manifest that asks for more than the operator funds is
    clamped rather than obeyed, and a manifest that declares nothing is bounded exactly as it was
    before this field existed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    # The advertised tool name; authorization gates and profile narrowing address this string.
    name: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    workflow: str = Field(min_length=1)
    # The first line of the generated tool's docstring; `description` carries the rest. Bounded
    # because both go verbatim into the prompt on every call, and an out-of-tree bundle is outside
    # `tests/test_context_floor.py`'s reach.
    summary: str = Field(min_length=1, max_length=MAX_MANIFEST_TEXT_CHARS)
    description: str = Field(default="", max_length=MAX_MANIFEST_TEXT_CHARS)
    # Launch arguments, declared one of two ways: `params` for a few flat, closed-type arguments, or
    # `params_model` (`module:Attribute`) for a rich domain object whose existing pydantic model
    # should not be re-declared in YAML. Neither means no arguments.
    params: list[JobParam] = Field(default_factory=list)
    params_model: str | None = Field(default=None, pattern=r"^[\w.]+:[A-Za-z_]\w*$")
    # A domain precondition checked before any durable work starts: a `module:function` taking the
    # validated params and raising to refuse the launch. The launch boundary is the only replay-safe
    # place for a rule that reads current config: a validator or an in-workflow check would re-run
    # during replay and could fail an in-flight run that was legal when it started.
    precondition: str | None = Field(default=None, pattern=r"^[\w.]+:[A-Za-z_]\w*$")
    # Whether this deployment can run the job at all: a `module:function` taking nothing and
    # returning a reason why not, or `None`. A job with a reason is withheld from the model
    # (`registry.job_tools`) and refused on every other launch path (`jobs.prepare_job_launch`).
    unavailable_reason: str | None = Field(default=None, pattern=r"^[\w.]+:[A-Za-z_]\w*$")
    expensive: bool = False
    publish_to_graph: bool = False
    # What this job changes outside this deployment, and whether it can be undone. Absent means its
    # writes are this system's own.
    effect: EffectSpec | None = None
    # How long the launcher waits for the run before handing back a job id instead; unset means
    # always a job. Lets one tool serve a seconds-long input inline and a minutes-long one as a job,
    # decided by elapsed time rather than a cost prediction that would put chemistry back in core.
    # Keep it well under `service_turn_timeout_seconds`.
    inline_wait_seconds: float | None = Field(default=None, gt=0)
    # A ceiling on this job's whole durable run, in seconds, that can only lower the deployment's:
    # the effective ceiling is `min(this, connector_job_timeout_seconds)`
    # (`durable/connector_job.py::child_execution_timeout`), so a bundle can never grant itself more
    # runtime. Unset means the deployment's ceiling.
    #
    # Never set it below the longest activity its workflow runs: core cannot check that, and a
    # single attempt would exhaust the ceiling and make retries unreachable.
    timeout_seconds: float | None = Field(default=None, gt=0)
    # Whether this job suspends on a person (`durable/awaiting.py`), so elapsed time is not cost:
    # `child_execution_timeout` then gives it no ceiling. Safe to declare because it describes the
    # job's shape rather than buying compute (each activity keeps its own budget), and it requires
    # the operator's grant (`jobs.require_funded_ceiling`).
    awaits_answer: bool = False

    @model_validator(mode="after")
    def _a_job_that_waits_does_not_also_declare_what_it_costs(self) -> Self:
        """Reject `awaits_answer` beside `timeout_seconds`: the two say opposite things.

        Honouring one silently would make the other a key that reads like a control and is not, and
        which one the author meant is not for a resolver to guess.
        """
        if self.awaits_answer and self.timeout_seconds is not None:
            raise ValueError(
                f"job {self.name!r} declares `awaits_answer: true` and "
                f"`timeout_seconds: {self.timeout_seconds}`; a job that suspends on a durable "
                "answer has no wall-clock ceiling to declare, so keep one or the other"
            )
        return self

    @model_validator(mode="after")
    def _effects_are_gated(self) -> Self:
        """A job that changes somebody else's system is expensive by declaration, not by choice.

        `expensive` puts a job behind `authorize_trigger`. Refused rather than corrected, since the
        author believed one of the two fields and which one matters.
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
    core graph-write tool. That is the existing `allowed_tools` boundary (D-029) promoted from a
    convention to a validated contract: `make connector-validate` refuses a name matching the
    mutating-tool prefixes, so a connector cannot quietly hand the model a write path.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9-]*$")
    description: str = Field(min_length=1, max_length=MAX_MANIFEST_TEXT_CHARS)
    endpoint: Endpoint | None = Field(default=None, discriminator="transport")
    jobs: list[JobSpec] = Field(default_factory=list)
    # Names of the `SKILL.md` folders under this bundle's `skills/` and the profile files under its
    # `profiles/`. Declared so a stray folder fails CI rather than shipping
    # (`scripts.validate_connectors`).
    skills: list[str] = Field(default_factory=list)
    profiles: list[str] = Field(default_factory=list)
    # The knowledge-graph vocabulary this bundle's `publish_to_graph` jobs mint, unioned into
    # `KNOWN_NOTE_TYPES`/`KNOWN_RELATIONS` by `chemclaw.kg.note.known_note_types` and its sibling.
    # The vocabulary stays closed (an undeclared name still fails `make kg-validate`), but a bundle
    # can extend it without a core edit. Validated for shape only.
    note_types: list[str] = Field(default_factory=list)
    relations: list[str] = Field(default_factory=list)
    # Whether an empty `connectors_enabled` turns this bundle on. Declaring a capability and binding
    # it are separate decisions: a manifest is read by the validators whether or not a turn binds
    # it, but every bound tool's schema is charged on every model call
    # (`tests/test_context_floor.PREFIX_BOUND`). A costly optional bundle declares `false` and a
    # deployment that wants it names it in `CHEMCLAW_CONNECTORS_ENABLED`; `connectors_enabled`
    # remains the single switch.
    default_enabled: bool = True

    @model_validator(mode="after")
    def _vocabulary_is_well_formed(self) -> Self:
        """Reject a note type or relation that is not a lowercase hyphenated token.

        These names become path segments and frontmatter keys, so anything else would produce a note
        that validates and then cannot be found by the filters keyed on it.
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

    @model_validator(mode="after")
    def _compensations_name_a_declared_job(self) -> Self:
        """A named compensation must be a job this bundle actually declares.

        Nothing runs a compensation automatically; the name tells an operator which job undoes this
        one, so it must resolve. Checked here because a job cannot see its siblings.
        """
        declared = {job.name for job in self.jobs}
        unresolved = sorted(
            f"{job.name!r} -> {job.effect.compensation!r}"
            for job in self.jobs
            if job.effect is not None
            and job.effect.compensation
            and job.effect.compensation not in declared
        )
        if unresolved:
            raise ValueError(
                f"connector {self.name!r} names compensation(s) it does not declare as jobs: "
                f"{', '.join(unresolved)} — a compensation is launched like any other job, so a "
                "name outside this bundle is one nobody can run"
            )
        return self
