"""What one data source declares about the labels it carries — the `labels:` manifest block.

This is the answer to "how do we configure the enrichment per database". It is a block in each
source's own `datasource.yaml`, not a field in `Settings`, because `core/config/README.md` states
the rule plainly: *config says which and where; a manifest says what.* Which sources exist is
`CHEMCLAW_DATA_SOURCES`; what a given source already knows about its own rows is that source's
business, and putting it in config would mean one global list keyed by source name — a second place
to forget.

**`provides` is never a skip.** That is the whole point, and it is what the request "the database
will not have all these labels in the beginning, so the agent should be able to identify all"
actually demands. Pistachio ships NameRxn names for roughly two thirds of its rows, not for all of
them; an ELN ships none. A group listed in `provides` is still derived for every row where the
source left it empty. It is read for exactly two things: the coverage report an answer carries
("this source provided a name for 62% of its rows; the labeller derived the rest"), and the subset
check on `override`.
"""

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from chemclaw.science.labels.vocabulary import LabelGroup


class LabelPolicy(BaseModel):
    """One source's declaration: what it carries natively, and what to re-derive anyway."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provides: frozenset[LabelGroup] = Field(
        default_factory=frozenset,
        description=(
            "Groups this source is expected to carry. Never a skip — a row where the source left "
            "the group empty is still derived. A claim about the source's intent, not a promise "
            "about any row."
        ),
    )
    override: frozenset[LabelGroup] = Field(
        default_factory=frozenset,
        description=(
            "Groups re-derived even where the source did supply a value. Exists because an ELN's "
            "roles are a free-text column somebody typed: `species-roles` from such a source is a "
            "five-value guess the refined vocabulary must not inherit."
        ),
    )

    @model_validator(mode="after")
    def _override_is_a_subset_of_provides(self) -> Self:
        """Reject overriding a group the source never provides — a no-op that reads as a policy.

        The failure it prevents is not a crash but a misreading: `override: [named-reaction]` on a
        source that provides none looks, to the next person, like the source's names are being
        distrusted, when in fact nothing is being overridden because there is nothing there.
        """
        stray = self.override - self.provides
        if stray:
            names = ", ".join(sorted(g.value for g in stray))
            raise ValueError(
                f"`override` lists {names}, which `provides` does not — overriding a group the "
                "source never supplies changes nothing; remove it, or add it to `provides`"
            )
        return self

    def derives(self, group: LabelGroup, has_value: bool) -> bool:
        """Whether the enricher should derive `group` for a row that does/doesn't already hold it.

        The single expression of the merge rule, so the drain and the coverage report cannot
        disagree about what "missing" means.
        """
        return group in self.override or not has_value
