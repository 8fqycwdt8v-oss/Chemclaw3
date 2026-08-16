"""Map a finished QM calculation to a knowledge-graph note (plan step 2.8).

A completed calculation becomes an agent-authored `job-result` note and is proposed through the
**same** PR-gate every other agent note uses (D-005) — there is no second write path.

This module is the *mapping only*, which is the connector split (the shape `connectors/bo/
knowledge.py` already has): turning a QM result into a note is this domain's knowledge, so it lives
in the bundle; pushing that note through the PR-gate is the review boundary, so it stays in core —
`ConnectorJobWorkflow` publishes whatever note the result envelope carries, via the one
`publish_memory_note_activity`. The `write_knowledge_node` activity that used to do both is gone: a
connector must not be able to reach around the gate, and now it structurally cannot (D-118).
"""

from chemclaw.connectors.qm.specs import QMJobResult, QmJobSpec, qm_job_key
from chemclaw.core.chem import compound_id
from chemclaw.kg.note import Note
from chemclaw.science.calc.uncertainty import Estimate


def qm_energy_estimate(result: QMJobResult) -> Estimate:
    """A finished energy in the uniform trust shape (F8-T1) — what a note or a summary renders.

    **Here rather than on `QMJobResult`**, which would read more naturally and is exactly the
    mistake `tests/test_connector_isolation.py` exists to catch: `specs.py` is the leaf module
    `connector.yaml` names as `params_model`, so the chat service imports whatever it imports, and
    reaching into `chemclaw.science` from there is how `connectors/calc/specs.py` once dragged four
    science modules into every `build_langgraph_agent` (D-118). This module is already past that
    boundary.

    **`uncertainty` is `None`, and that is the answer rather than a gap awaiting a number.** An
    absolute total energy has no meaningful error bar — as this repo already says where it
    differences them (`chemclaw.connectors.calc.compose`): the method and basis-set error is
    enormous
    in absolute terms and cancels almost entirely in a difference. Inventing a figure here would be
    a fabricated uncertainty, refused for the reason D-2026-08-01 refused fabricated domain bounds —
    a number in a validated record is expected to have a provenance, and a check that exists gets
    trusted.

    **Convergence is the domain question.** `in_domain` asks whether the model could speak about
    this case at all, and an SCF that did not converge never produced an answer it can stand behind.
    Carrying that as the uniform flag is what lets a consumer which has never heard of an SCF — a
    skill, a note writer, a retrieval excerpt — decline the number.
    """
    return Estimate(
        value=result.total_energy_hartree,
        unit="Hartree",
        method="none",
        in_domain=result.converged,
        domain_reasons=()
        if result.converged
        else (
            "the SCF did not converge, so this is where the optimizer stopped rather than a "
            "stationary point of the method",
        ),
    )


def note_from_qm_result(result: QMJobResult, calc_key: str = "") -> Note:
    """Map a QM job result to an agent-authored `job-result` note.

    **The note also links its calculation** when `calc_key` is given (D-158) — the flat
    `CalculationKey` string the persist activity returns, recorded in `calc_refs`. That is the
    second half of the same graph-island problem the compound link below closed: `calc_refs` and
    `chemclaw.kg.crosslink` (`cited_calculations`, `notes_for_calculation`) have existed since
    D-133 with **no producer anywhere in `src/`**, so the read side was complete and unreachable.
    A QM result is the natural first writer, being the most expensive number the system holds.

    Empty when the persist step is disabled or did not complete, because a reference to a row that
    was never written would fail `chemclaw.kg.validate` on the very PR this note opens.

    **The note links its compound.** It used to refuse to, and said so: a wikilink to a compound
    note that might not exist yet would dangle and fail `chemclaw.kg.validate` on the very PR this
    opens.
    The consequence was that every computed result was a graph island — the calculation store and
    the knowledge graph, the two halves of the system's memory, could not reference each other in
    either direction (STO-7).

    What changed is not this module's confidence but the PR-gate's shape: a `NoteSubmission` now
    carries a note *with its dependencies*, and
    `chemclaw.ingest.eln.compound.compound_dependencies` mints the
    compound note into the same PR. So the link resolves on the branch it is proposed on. Because
    `compound_id` is derived from the canonical structure, the target here is the same id that
    helper will produce — one derivation, used twice.

    **The energy line carries its own trust** (F8-T1). It used to be a bare
    `total energy: {x:.6f} Hartree`, which is the shape a retrieval excerpt quotes back with
    nothing attached — a confident number, indistinguishable from one the SCF never converged to.
    It now renders through `Estimate`, which keeps the statement *on* the value line, where the
    excerpt's blind character prefix cannot separate them. `converged` stays as its own line: it is
    the primitive fact, and the estimate is the reading derived from it, not a replacement.

    The note id is the calculation key, so re-writing the same calculation is idempotent.
    """
    spec = QmJobSpec(
        molecule_smiles=result.molecule_smiles,
        method=result.method,
        basis_set=result.basis_set,
    )
    body = (
        f"Calculation for [[{compound_id(result.molecule_smiles)}]] "
        f"(`{result.molecule_smiles}`), method {result.method}/{result.basis_set}.\n\n"
        f"- total energy: {qm_energy_estimate(result).render(fmt='.6f')}\n"
        f"- converged: {result.converged}\n"
    )
    return Note(
        id=f"job-{qm_job_key(spec)}",
        type="job-result",
        compound_smiles=result.molecule_smiles,
        created_by="agent",
        source=f"qm:{result.requested_by}",
        calc_refs=[calc_key] if calc_key else [],
        body=body,
    )
