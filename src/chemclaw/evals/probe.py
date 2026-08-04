"""The live-probe declaration: one user question and how to tell whether the answer served it.

Separate from `chemclaw.evals.metric`'s `EvalCase` on purpose. An `EvalCase` scores a value that
has *already been produced* — it is a pure function over recorded output. A `Probe` is the input
to a live conversation that has not happened yet: it names a question to ask a running system and
the evidence that would make its answer acceptable. Folding the two together would put an HTTP
round trip inside a pure metric.

The fields encode the one thing a scripted-transcript eval cannot check (`DEFERRED.md`, AG-13):
whether the *model* reached for the capability the system actually has. `expects_tools` makes
"it never called the tool that exists" a mechanical observation over the event stream rather than
a judgement about prose, and `forbids_claims` makes the opposite failure — asserting a capability
the system does not have — equally mechanical to raise, though it takes a judge to settle.

`bucket` records what we knew before asking, so a run reports coverage honestly. A `C` probe that
is answered with a clear refusal is a **pass**: the system behaving correctly at its own edge.
Grading a known-absent capability as a failure would make the score a measure of the tool list.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Persona = Literal["lab_technician", "lab_leader", "manager"]

# A = the capability exists and the probe should exercise it.
# B = a substrate exists but the specific ask does not; a good answer is partial and says so.
# C = no capability at all; a good answer is an honest refusal plus what it *can* do.
Bucket = Literal["A", "B", "C"]


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
    # True when a satisfying answer requires a *durable* job to have actually run — not merely a
    # tool named in `expects_tools` to have been called. The distinction is the whole reason this
    # field exists: a job tool returns a workflow id the moment the launch is accepted, so an
    # answer can report a started job that Temporal never ran, and every signal derived from the
    # event stream alone would score it as success. Marking a probe here lets the runner ask the
    # broker for the workflow's terminal state instead of believing the turn's account of it.
    #
    # A bool rather than a job name: the probe is a *question*, and naming the job it must reach
    # would grade the model's routing taste, which is the same argument `expects_tools` settles by
    # being any-of.
    expects_job: bool = False
    forbids_claims: list[str] = Field(default_factory=list)
    direction: str = Field(min_length=1)


class ProbeSet(BaseModel):
    """A probe file: `probes:` and nothing else, so a stray top-level key is a loud error."""

    model_config = ConfigDict(extra="forbid")

    probes: list[Probe] = Field(min_length=1)
