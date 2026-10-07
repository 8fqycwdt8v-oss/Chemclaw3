"""The live-probe declaration: one user question and how to tell whether the answer served it.

Separate from `EvalCase`, which scores output already produced: a `Probe` is the input to a live
conversation. `expects_tools` makes "never called the tool that exists" a mechanical observation
over the event stream; `forbids_claims` names the opposite failure, claiming a capability the system
lacks, for a judge to settle.

`bucket` records what was known before asking: a bucket-C probe answered with a clear refusal is a
pass. `follow_ups` turns a probe into a scripted conversation (later turns in the same session, each
naming the human act before it), which the plan-gate measurement needs; a probe without them is a
single question.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Persona = Literal["lab_technician", "lab_leader", "manager"]

# What a human does *between* two turns of a scripted probe.
#
# `approve_plan` reads the session's current plan (`GET /sessions/{id}/plan`) and posts a yes
# against the hash the server reports, since approvals bind to a plan identity
# (`agent/plan_gate.plan_identity`). `none` is the ordinary case; it is how a probe changes the plan
# without approval to check re-gating.
Intervention = Literal["none", "approve_plan"]


class Turn(BaseModel):
    """One later turn of a scripted probe: what is said, and what a human did first."""

    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1)
    before: Intervention = "none"


# A = the capability exists and the probe should exercise it.
# B = a substrate exists but the specific ask does not; a good answer is partial and says so.
# C = no capability at all; a good answer is an honest refusal plus what it *can* do.
Bucket = Literal["A", "B", "C"]

# The prefix that marks an `asserts_absent` entry as a capability no tool name reaches. One constant
# so writer and reader agree; upper case and hyphenated so it cannot collide with a lower-snake-case
# tool name.
ABSENT_MARKER = "NO-TOOL "


class Probe(BaseModel):
    """One question to ask a live system, with the direction a satisfying answer would take.

    Graded against a *direction* rather than a key because a real user does not know the answer;
    they know what a useful answer looks like. That is the precedent this run inherits from the
    fifty-question pass recorded in `docs/archive/vibe-test-2026-07.md`.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    section: int = Field(ge=1, le=17)
    persona: Persona
    bucket: Bucket
    question: str = Field(min_length=1)
    # Any-of, not all-of: several tools can legitimately serve one question, and demanding a
    # specific one would grade the model's routing taste rather than the system's reach.
    expects_tools: list[str] = Field(default_factory=list)
    # The connector bundle `expects_tools` is conditional on, when those tools come from a bundle
    # this repository does not declare (e.g. fleet servers reached by pointing
    # `CHEMCLAW_CONNECTORS_DIR` at `Chemclaw3-mcp`'s `manifests/`).
    #
    # A probe's bucket depends on the deployment: with the bundle bound the tool expectation
    # applies; without it the probe degrades to its bucket-C form and expects no tool
    # (`evals/live.py`). Only the tool expectation is conditional, never `forbids_claims`: a claim
    # worth forbidding is forbidden in both lanes.
    needs_bundle: str | None = None
    # What a bucket-C probe claims this system cannot do, named so the claim can be resolved
    # against the surface rather than read from `direction:` by a human.
    #
    # Two forms:
    #
    # - **A tool name** is checked against `available_tool_names()`; the probe fails when the
    #   surface binds it (the rule `PromptBlock.absent_unless` applies to the system prompt's
    #   denials). `tests/test_probe_coverage.py` also checks unbound names for likely typos.
    # - **`NO-TOOL <what is absent>`** covers absences no tool name reaches. It is checked for
    #   being a real phrase that names no bound tool; beyond that a reviewer is the check.
    #
    # Required on bucket C, permitted on B, refused on A — enforced by tests rather than
    # validators, because archived transcripts predating this field must still rehydrate.
    asserts_absent: list[str] = Field(default_factory=list)
    # True when a satisfying answer requires a durable job to have actually run, not merely been
    # launched: a job tool returns an id on acceptance, so the runner asks the broker for the
    # workflow's terminal state. A bool, not a job name, so the model's routing choice is not
    # graded.
    expects_job: bool = False
    # The `knowledge/` note ids a correct answer's retrieval should have returned — a gold set
    # scored as recall. All-of, unlike `expects_tools`: notes are not interchangeable (a probe may
    # need both a current and a retired playbook to tell them apart).
    expects_notes: list[str] = Field(default_factory=list)
    forbids_claims: list[str] = Field(default_factory=list)
    direction: str = Field(min_length=1)
    # Later turns of the *same* session, in order, each naming the human act that precedes it.
    # Empty for every probe in the shipped corpus, which is why adding this changed nothing there.
    follow_ups: list[Turn] = Field(default_factory=list)


class ProbeSet(BaseModel):
    """A probe file: `probes:` and nothing else, so a stray top-level key is a loud error."""

    model_config = ConfigDict(extra="forbid")

    probes: list[Probe] = Field(min_length=1)
