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
   GxP boundary; a connector reaches it only by returning a `Note` in a job envelope, which is a
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
in `core/config.py` — which is the whole point, and is now the rule rather than this
file's preference: config says which attached things exist and where, a manifest says what
each one is
(D-118, D-120). The two config-side unions this docstring used to cite as precedent,
`McpServerSpec` and `DataSourceSpec`, were both replaced by manifests for that reason.

Adding a transport or an auth mode is one variant plus one branch at the single dispatch site,
never a widening of one model with optional fields that only apply sometimes.
"""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

# A job parameter's declared type, mapped to a Python annotation by `connectors.jobs`.
# Deliberately a *closed* set: the generated pydantic model becomes the JSON schema the model
# fills in, and a schema the model can always fill correctly is worth more than an open type
# language. These cover every launch argument the existing durable jobs take (SMILES strings,
# method names, counts, flags, lists of either, and one nested spec object).
JobParamType = Literal["string", "integer", "number", "boolean", "string[]", "number[]", "object"]


class NoAuth(BaseModel):
    """No credential — the connector is inside our own trust boundary.

    Correct for a stdio connector (a subprocess of our own pod, under our own identity) and for
    a loopback HTTP connector in dev. `ConnectorManifest` refuses it for a non-loopback URL
    unless the deployment has explicitly opted into insecure binding, reusing the front door's
    loopback rule rather than inventing a second notion of "safe address".
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
    `request_timeout` (whole seconds, as MAF types it) keeps an unreachable host from hanging a
    turn; `None` defers to MAF's own default rather than inventing a number here.
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
    """
    classified = set(state_changing) | set(read_only)
    served = set(tools)
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
# one branch in `connectors.registry._mcp_tool`. Both variants carry `tools` — the agent-facing
# allow-list — because it is a property of *an endpoint's* surface: nesting it here rather than
# at the manifest's top level makes "an allow-list with no endpoint to serve it" unrepresentable
# instead of something a validator has to catch.
#
# `state_changing` names the subset of `tools` that spends real resources or writes data a person
# would care about — the ones the harness's plan gate refuses under an unapproved plan (D-167).
# It is declared **here, by the bundle**, and not as a list in core, for the same reason the queue
# and the params model are: whether `predict_pka` is a lookup or a calculation is the capability's
# own fact, and a copy of it in core is a second source of truth that goes stale the first time a
# bundle changes what a tool does. An undeclared tool is treated as a read: core cannot infer a
# bundle's semantics, and guessing "write" would gate every connector's whole surface the day this
# shipped.
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

    `expensive` puts the job in the coarse `authorize_trigger` gate's set (a costly HPC/BO run must
    be entitled, not merely authenticated) — the declaration *is* the gate's source, read by
    `chemclaw.agent.authz.expensive_actions`, so it needs no matching operator entry and gains
    nothing from one. It was for a while a marker that authorized nothing, because the gate
    consulted only `entra_expensive_actions`; `tests/test_authz.py` now cross-checks every declared
    job against the effective set. `publish_to_graph` lets core PR-gate a `Note` the job's result
    carries — the write still goes through `chemclaw.kg.pr_gate`, never through the connector.
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
