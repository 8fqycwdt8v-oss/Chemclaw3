"""The client for the reaction labeller: ask it what version it is, then ask it for the labels.

The models that do the labelling — RXNMapper for the atom map, RDKit's reaction-role assignment,
a curated agent dictionary for solvent/catalyst/ligand/base/additive, Rxn-INSIGHT's 527 curated
SMIRKS for the name — live in `Chemclaw3-mcp`'s `servers/rxnlabel`, not here. That is the
`D-2026-08-16-the-physics-leaves-the-cache-stays` split applied a second time and for the same
reason: RXNMapper is a transformer, so keeping it in-process would put torch and transformers into
every chat pod, and the labelling itself is a stateless primitive whose identity is derivable from
its inputs. What stays here is the index, the drain and the search — the parts that are about *our*
corpus rather than about chemistry.

**Nothing here derives the labeller version.** It is asked for, and that is the point rather than
an implementation detail: the version is half of what decides whether a row is stale, and it is
built from a model checkpoint hash, a SMIRKS file and an agent dictionary that this process cannot
see. A locally-derived one would be *well-formed* and would match nothing — every row would look
stale forever, the drain would re-label the whole corpus on every pass, and nothing would raise.
`connectors/calc/remote.py` records the same rule with the same reasoning.

What this side *does* fold in is the two versions the server cannot see: `STANDARDIZATION_VERSION`,
because the species SMILES we send it were normalised by our rules, and `VOCABULARY_VERSION`,
because the role names we store are ours. `remote_key` folds `CALCULATION_EPOCH` in on this side
for exactly that reason.

**The batch tools are the ones the drain calls.** A 13M-row corpus at one round trip per reaction
is 13M round trips; at `label_batch_size` it is 65,000. The single-reaction tools exist on the
server for a person asking about one reaction, and are not called from here.

**Species are sent explicitly rather than parsed out of the reaction SMILES.** The record form is
`reactants>agents>products`, and a stored species' `ordinal` comes from `OrdReaction.compounds()`
(inputs then outcomes, agents wherever they sat among the inputs) — the two orders are not the
same, so matching a returned role back onto a row by position would silently mislabel every
reaction with a solvent. Sending the list makes the request unambiguous and the response
positional against something we chose.
"""

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

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


class LabelServerError(SubsystemUnavailableError):
    """The labelling server could not be reached or fell over, so nothing was labelled.

    Retryable — deliberately absent from `durable/publish.py`'s non-retryable list — because the
    only thing that fixes an unreachable pod is trying again once it is back. The message is
    written for whoever reads the drain's logs: labelling is a background service, so unlike a
    calculation there is no chemist waiting on this particular call.
    """


class LabelToolError(ChemclawError):
    """The labelling server was reached and refused, or answered something unusable.

    Bad data by the same test as every other `ChemclawError`: a reaction SMILES RDKit cannot parse,
    a species list that does not match the reaction. The identical call fails identically, so it is
    registered non-retryable in `durable/publish.py::_BAD_DATA_TYPES` and the drain drops that one
    reaction rather than paying for the same refusal three more times.
    """


class SpeciesRepresentation(BaseModel):
    """What the labeller concluded about one species, positional against the list it was sent."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    role: str = Field(min_length=1, description="A `SpeciesRole` value, or 'unknown'.")
    scaffold: str | None = None
    functional_groups: list[str] = Field(default_factory=list)


class ReactionRepresentation(BaseModel):
    """The reaction-level representation: the atom map, and one entry per species sent."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    id: str = Field(min_length=1)
    mapped_smiles: str | None = None
    species: list[SpeciesRepresentation] = Field(default_factory=list)


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


@runtime_checkable
class Labeller(Protocol):
    """What the drain needs of a labelling server: a version, representations, names.

    A `Protocol` and not the class below, because there really are two implementations and the
    second must not inherit the first: a test's fake answers from fixtures and has no session, no
    credential and no transport to stub out. The same call `FingerprintStore` makes, and the
    opposite of the one `LabelIndex` makes — there a Protocol would have bought structural typing
    nobody uses.
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

    A session per call rather than one per process, for the reason `connectors.identity` records:
    the MCP transport's tasks inherit the context of whoever opened the connection, so a shared
    session misattributes concurrent callers to each other. The cost is a connect per batch, which
    is noise against a batch of 200 atom mappings.
    """

    async def version(self) -> str:
        """The identity a row is stamped with — the server's, plus the two versions it cannot see.

        Our standardization version rides along because the species SMILES sent for classification
        were normalised by our rules, and our vocabulary version because the role names stored are
        ours: a change to either means the stored labels no longer mean what a fresh call would
        return, which is precisely what "stale" has to catch.
        """
        payload = await self._call("labeller_version", {})
        remote = str(payload.get("version") or "").strip()
        if not remote:
            raise LabelToolError(
                "the labelling server reported no version, so nothing could be stamped. A row's "
                "staleness is decided by that string; without it every row would be re-labelled "
                "on every pass forever."
            )
        return f"{remote}:{STANDARDIZATION_VERSION}:{VOCABULARY_VERSION}"

    async def represent(
        self, reactions: list[tuple[str, str, list[str]]]
    ) -> dict[str, ReactionRepresentation]:
        """Atom-map and role-assign a batch, keyed by the ids given.

        Args:
            reactions: `(id, record_smiles, species_smiles)` per reaction. The species list is
                positional and comes back in the same order — see the module docstring for why it
                is sent rather than parsed out of the reaction.

        Returns:
            One representation per id the server answered for. A reaction the server could not
            represent is simply absent, so the caller can record what it did get; the drain treats
            a missing entry as "not labelled this pass" rather than as an error.
        """
        payload = await self._call(
            "represent_reactions",
            {
                "reactions": [
                    {"id": rid, "reaction_smiles": smiles, "species": species}
                    for rid, smiles, species in reactions
                ]
            },
        )
        return {
            item.id: item
            for item in (ReactionRepresentation.model_validate(r) for r in _results(payload))
        }

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

        The transport, the timeout ordering, the credential-rejection walk and the internal-error
        string are all `core.mcp_session`'s. What is decided here is the one thing that differs
        between services: which failures a durable activity should retry.
        """
        try:
            async with open_session(
                settings.rxnlabel_server_url,
                token_env=settings.rxnlabel_server_token_env,
                timeout_seconds=settings.rxnlabel_server_timeout_seconds,
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

    Checked rather than defaulted to empty, because an empty batch answer and a malformed one look
    identical to the drain — it would record zero labels, report progress, and advance.
    """
    results = payload.get("results")
    if not isinstance(results, list):
        raise LabelToolError(f"the labelling server answered no `results` list: {payload!r:.200}")
    return results
