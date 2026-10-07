"""What one data source declares about the labels it carries — the `labels:` manifest block.

A block in each source's `datasource.yaml`, not in `Settings`: config says which sources and where;
a manifest says what a source knows about its own rows.

`provides` is never a skip. A source may supply a label for only some rows, so a provided group is
still derived wherever the source left it empty. `provides` feeds the coverage report and the subset
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
        """Reject overriding a group the source never provides — a no-op that reads as a policy."""
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

        The single expression of the merge rule, so the drain and the coverage report agree on what
        "missing" means.
        """
        return group in self.override or not has_value
