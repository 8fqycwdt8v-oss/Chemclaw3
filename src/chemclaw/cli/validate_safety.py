"""Force-compile the safety tables at CI/deploy time, instead of on the first hazard question.

`science/safety/screen.py::_load_rules` and `science/safety/genotox.py::_load_alerts` are both
`@lru_cache`d and lazy: nothing calls either at import or process startup. They fail loudly and
correctly on a malformed table or an unparseable SMARTS (`SafetyRulesError`, deliberately fatal —
see both modules' docstrings) — but only the first time something actually screens a molecule. So a
shipped YAML typo, or one bad SMARTS in a table with sixteen-plus rules, would surface as a runtime
exception the moment a chemist first asked a hazard question in production, which is the one
failure shape a *citable* safety table exists to prevent.

**Why a validator rather than an eager load in the connector lifespan.** `connectors/server.py`'s
`connector_app` lifespan is the only hook in the system, and every bundle shares it — eager-loading
the safety tables there would make every non-safety bundle (calc, bo, qm, ...) pay a RDKit SMARTS
compile on every boot for a table it never reads. A bundle-specific lifespan seam would scope that
correctly, but it is new machinery built for exactly one caller (`connectors/safety/server/app.py`),
which is the abstraction CLAUDE.md's Rule of Three says to inline, not build. A validator run at
CI/deploy time gets the "before a user's first hazard question" guarantee these two other options
either overpay for or under-build for, with no runtime cost and no new seam: this is `make
safety-validate`, wired into CI the same way every other `*-validate` gate is (`kg-validate`,
`skill-validate`, ...), so a bad table fails the build rather than the first live screen.

Screening `"C"` (methane) rather than reaching into the modules' private loaders: both
`_load_rules` and `_load_alerts` compile *every* rule's SMARTS up front, matched or not (each
docstring says so — "so every SMARTS is compiled exactly once per process"), so one call through
the public API exercises the exact code path a real hazard question takes and validates the whole
table, not just the rules that happen to match the probe molecule.
"""

from chemclaw.core.errors import ChemclawError
from chemclaw.science.safety.genotox import screen_genotoxic_alerts
from chemclaw.science.safety.screen import screen_reaction

# A minimal, always-parseable molecule. What it screens as does not matter — only that screening it
# forces both tables to fully parse and every SMARTS in them to compile.
_PROBE_SMILES = "C"


def validate_safety() -> list[str]:
    """Return a list of problems compiling the safety tables (empty = both tables are sound)."""
    problems: list[str] = []
    try:
        screen_reaction([_PROBE_SMILES])
    except ChemclawError as exc:
        problems.append(f"process-safety rule table failed to compile: {exc}")
    try:
        screen_genotoxic_alerts([_PROBE_SMILES])
    except ChemclawError as exc:
        problems.append(f"genotoxicity alert table failed to compile: {exc}")
    return problems


def main() -> int:
    """Validate both safety tables; print problems and exit non-zero if either fails to compile."""
    problems = validate_safety()
    if problems:
        print("safety table validation failed:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("safety table validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
