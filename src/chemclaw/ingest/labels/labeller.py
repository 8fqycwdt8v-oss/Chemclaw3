"""The client for the reaction labeller: ask it what version it is, then ask it for the labels.

The labelling models (RXNMapper, RDKit role assignment, an agent dictionary, curated SMIRKS) run in
`Chemclaw3-mcp`'s `servers/rxnlabel`, keeping torch out of every chat pod; the index, drain and
search stay here.

The labeller version is asked for, never derived: it depends on checkpoints and files this process
cannot see, and a locally derived one would match nothing and re-label the corpus forever. This side
folds in only `STANDARDIZATION_VERSION` (we normalised the SMILES sent) and `VOCABULARY_VERSION`
(the stored role names are ours).

The drain calls the batch tools. Species are sent explicitly rather than parsed from the reaction
SMILES, because stored ordinals follow `OrdReaction.compounds()` order, which differs from the
record SMILES; answers are then positional against the list we sent.
"""

import logging
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from chemclaw.core.call_identity import turn_identity_hook
from chemclaw.core.chem import STANDARDIZATION_VERSION
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError, SubsystemUnavailableError
from chemclaw.core.mcp_session import (
    McpConnectFailed,
    McpCredentialRefused,
    McpRequestRefused,
    McpServerFault,
    invoke,
    open_session,
)
from chemclaw.science.labels.vocabulary import VOCABULARY_VERSION

logger = logging.getLogger(__name__)


class LabelServerError(SubsystemUnavailableError):
    """The labelling server could not be reached or fell over, so nothing was labelled.

    Retryable: only trying again once the pod is back fixes it. The message is written for the
    drain's logs, since no chemist is waiting on this call.
    """


class LabelToolError(ChemclawError):
    """The labelling server was reached and refused, or answered something unusable.

    Bad data (an unparseable SMILES, a mismatched species list): the identical call fails
    identically, so it is non-retryable (`durable/publish.py::_BAD_DATA_TYPES`) and the drain drops
    that one reaction.
    """


class SpeciesRepresentation(BaseModel):
    """What the labeller concluded about one species, positional against the list it was sent."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    role: str = Field(min_length=1, description="A `SpeciesRole` value, or 'unknown'.")
    scaffold: str | None = None
    functional_groups: list[str] = Field(default_factory=list)


class ReactionRepresentation(BaseModel):
    """The reaction-level representation: the atom map, and one entry per species sent.

    **`version` and `degraded` are read from the answer rather than assumed from the pass**, and
    that is what makes a degraded row re-labellable. `Chemclaw3-mcp`'s `servers/rxnlabel` answers
    each reaction with `version.labeller_version(degraded)` — a stamp naming the failed component
    (`mapper@failed`) rather than the healthy one — precisely so the row is stale against a pod
    whose mapper works. This model declares `extra="ignore"`, so both fields were *dropped in
    transit*: the drain stamped every row with the pass-level version it read once in
    `plan_label_sync`, which reports the components it *probed* and not what happened on this call.
    A mapper that was installed and failed therefore produced a row stamped healthy, which leaves
    `stale()` and is never revisited until the deployment's component versions change. See
    `enrich.label_stale` for the stamp and `_degradations` for the log.

    **`reaction_smiles` and `unreadable_species` are deliberately still ignored.** The server sends
    both and this side wants neither:

    * `reaction_smiles` is the server's own canonicalisation of the reaction. The reaction text this
      system stores is normalised by *our* rules, which is exactly what `STANDARDIZATION_VERSION`
      versions and why `version()` folds it in — adopting the server's form would put its RDKit
      build inside a field this repository versions itself, with nothing to notice.
    * `unreadable_species` names species strings RDKit could not read. They come from *our* corpus,
      so it is a data-quality fact about an ingested source rather than about this label — and the
      place to act on it is the ingest path that wrote those strings. Keeping it here would be a
      field with no consumer, and a per-row log of it is one line per bad record across a corpus
      sized in millions. The loss is not silent in the way
      `D-2026-08-08-a-partial-answer-must-say-so` is about: an unreadable species still comes back
      in `species` at its own position, so nothing positional shifts, and the short-answer guard in
      `represent` below is what covers the case where something does.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    id: str = Field(min_length=1)
    mapped_smiles: str | None = None
    species: list[SpeciesRepresentation] = Field(default_factory=list)
    # Defaulted empty, so a server predating these fields — or a fake that does not set them —
    # parses and falls back to the pass-level stamp, which is today's behaviour.
    version: str = Field(
        default="",
        description="The labeller that produced *this* answer; empty means the server sent none.",
    )
    degraded: list[str] = Field(
        default_factory=list,
        description="Components installed on that pod that ran and failed, e.g. 'atom_mapper'.",
    )


class ReactionNaming(BaseModel):
    """The classification: which named reaction this is, and how confidently, and by what route."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    id: str = Field(min_length=1)
    named_reaction: str | None = None
    reaction_class: str | None = None
    rxno_id: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    method: str | None = Field(
        default=None, description="'smirks' for a rule match, 'model' for the fallback classifier."
    )
    # As on `ReactionRepresentation`: these fields distinguish a classifier that matched nothing
    # (empty `degraded`) from one that failed.
    version: str = Field(
        default="",
        description="The labeller that produced *this* answer; empty means the server sent none.",
    )
    degraded: list[str] = Field(
        default_factory=list,
        description="Components installed on that pod that ran and failed, e.g. 'reaction_namer'.",
    )


def stamped(remote: str) -> str:
    """One remote labeller version, plus the two versions the server cannot see.

    The one definition of the fold, used for both the pass-level and per-answer stamps, so they can
    match. `STANDARDIZATION_VERSION` because we normalised the species SMILES sent;
    `VOCABULARY_VERSION` because the stored role names are ours.

    Args:
        remote: The version string the labelling server reported, as it reported it.
    """
    return f"{remote}:{STANDARDIZATION_VERSION}:{VOCABULARY_VERSION}"


@runtime_checkable
class Labeller(Protocol):
    """What the drain needs of a labelling server: a version, representations, names.

    A `Protocol` so a test fake needs no session, credential or transport.
    """

    async def version(self) -> str:
        """The identity a labelled row is stamped with."""
        ...

    async def represent(
        self, reactions: list[tuple[str, str, list[str]]]
    ) -> dict[str, "ReactionRepresentation"]:
        """Atom-map and role-assign a batch of `(id, record_smiles, species_smiles)`."""
        ...

    async def name(self, reactions: list[tuple[str, str]]) -> dict[str, "ReactionNaming"]:
        """Classify a batch of `(id, record_smiles)` into named reactions."""
        ...


class RxnLabelServer:
    """One drain's worth of calls to the labelling server, each in its own MCP session.

    A session per call because the MCP transport's tasks inherit the opener's context, so a shared
    session would misattribute concurrent callers. The connect cost is small against a batch.
    """

    async def version(self) -> str:
        """The identity a row is stamped with: the server's, plus the two versions it cannot see.

        A change to our standardization or vocabulary version also makes stored labels stale.
        """
        payload = await self._call("labeller_version", {})
        remote = str(payload.get("version") or "").strip()
        if not remote:
            raise LabelToolError(
                "the labelling server reported no version, so nothing could be stamped. A row's "
                "staleness is decided by that string; without it every row would be re-labelled "
                "on every pass forever."
            )
        return stamped(remote)

    async def represent(
        self, reactions: list[tuple[str, str, list[str]]]
    ) -> dict[str, ReactionRepresentation]:
        """Atom-map and role-assign a batch, keyed by the ids given.

        Args:
            reactions: `(id, record_smiles, species_smiles)` per reaction. The species list is
                positional and comes back in the same order.

        Returns:
            One representation per id the server answered for; a missing id means "not labelled this
            pass". An answer whose species list is neither empty nor the length sent has that list
            blanked: positional roles would otherwise shift onto the wrong molecules. The rest
            of the answer (e.g. `mapped_smiles`) is kept, and roles fall back to the source's
            coarse map.
        """
        sent = {rid: len(species) for rid, _smiles, species in reactions}
        payload = await self._call(
            "represent_reactions",
            {
                "reactions": [
                    {"id": rid, "reaction_smiles": smiles, "species": species}
                    for rid, smiles, species in reactions
                ]
            },
        )
        answers: dict[str, ReactionRepresentation] = {}
        for item in (ReactionRepresentation.model_validate(r) for r in _results(payload)):
            expected = sent.get(item.id)
            # An id this batch never sent is left to `enrich._placed`, which warns about it once.
            mismatched = (
                expected is not None and bool(item.species) and len(item.species) != expected
            )
            if mismatched:
                logger.warning(
                    "the labelling server answered for %d of the %d species sent for reaction %r; "
                    "the roles are matched back by position, so that half of the answer is "
                    "discarded and the species roles fall back to what the source recorded. The "
                    "atom map is not positional and is kept",
                    len(item.species),
                    expected,
                    item.id,
                )
            answers[item.id] = item.model_copy(update={"species": []}) if mismatched else item
        return answers

    async def name(self, reactions: list[tuple[str, str]]) -> dict[str, ReactionNaming]:
        """Classify a batch into named reactions, keyed by the ids given.

        Args:
            reactions: `(id, record_smiles)` per reaction.

        Returns:
            One naming per id the server answered for; absent means unclassified this pass.
        """
        payload = await self._call(
            "name_reactions",
            {"reactions": [{"id": rid, "reaction_smiles": smiles} for rid, smiles in reactions]},
        )
        return {
            item.id: item for item in (ReactionNaming.model_validate(r) for r in _results(payload))
        }

    async def _call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Open a session, invoke `tool`, and translate the failure into this service's vocabulary.

        Transport, timeouts and credential handling are `core.mcp_session`'s; this decides only
        which failures a durable activity should retry.
        """
        try:
            async with open_session(
                settings.rxnlabel_server_url,
                token_env=settings.rxnlabel_server_token_env,
                timeout_seconds=settings.rxnlabel_server_timeout_seconds,
                # Stamp the caller's identity and trace context on the call. The hook is bound to
                # this server's origin and strips on a cross-origin redirect.
                request_hook=turn_identity_hook(settings.rxnlabel_server_url),
            ) as session:
                payload = await invoke(session, tool, arguments)
        except McpCredentialRefused as exc:
            raise LabelToolError(
                f"the labelling server refused this client's credential (HTTP {exc.status} from "
                f"{settings.rxnlabel_server_url}). It is running and answering; it does not accept "
                f"the bearer taken from {settings.rxnlabel_server_token_env}. Retrying will not "
                "help — set that variable to the value the server verifies."
            ) from exc
        except McpConnectFailed as exc:
            raise LabelServerError(
                "the labelling server is not answering, so no reaction was labelled. The index is "
                "unchanged and the drain will pick these rows up again once it is back."
            ) from exc
        except McpRequestRefused as exc:
            raise LabelToolError(str(exc)) from exc
        except McpServerFault as exc:
            raise LabelServerError(
                f"the labelling server failed while running {tool}, so no reaction was labelled. "
                "This is a fault on that server rather than a problem with what was asked."
            ) from exc
        if not isinstance(payload, dict):
            raise LabelToolError(f"{tool} answered {type(payload).__name__}, expected an object")
        return payload


def _results(payload: dict[str, Any]) -> list[Any]:
    """The `results` list of a batch answer, or a refusal naming what came back instead.

    Checked rather than defaulted, since an empty and a malformed answer would otherwise look alike.
    """
    results = payload.get("results")
    if not isinstance(results, list):
        raise LabelToolError(f"the labelling server answered no `results` list: {payload!r:.200}")
    return results
