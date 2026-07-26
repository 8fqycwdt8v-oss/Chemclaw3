"""The `crest` binary: conformer, tautomer and protomer sampling (xTB plan X6).

CREST removes the caveat attached to every other number in this system. Everything else
describes **one** conformer — whichever geometry was embedded and relaxed — and for a
flexible molecule that is a shape, not the molecule. CREST searches the conformational
space by metadynamics and returns the ensemble with its populations, which is what turns
"the free energy of a conformer" into "the free energy of a compound".

Four searches, one binary:

- **conformers** — the ensemble and its Boltzmann populations. Measured on n-butane: two
  unique conformers, the anti at 59% — the textbook answer.
- **tautomers** — enumerate and rank tautomers, which is the one question in this system
  where getting it wrong silently invalidates *every* downstream number, because a pKa,
  a Fukui ranking and a reaction energy all describe whichever tautomer was drawn.
- **protomers** — where a molecule protonates or deprotonates, ranked.
- **complex** (`--nci`) — how *two* molecules associate. The only route in this system to
  a question about a pair rather than a species (`calc.complexes`).

**Licensing, stated once because it is a real decision and not an engineering one.**
CREST is GPL-3.0. It is invoked here as a separate process over files, never linked, so
the usual analysis is that it does not affect the licence of this codebase — but shipping
the binary in an image is a distribution question that belongs to whoever owns the
product, not to this module. `crest` is optional: absent, the ensemble tasks report that
they are unavailable and everything else works unchanged.

**Security.** Same rule as `calc.xtb_cli` and for the same reason: argv list,
`shell=False`, a fresh temporary directory, a scrubbed environment, a timeout, and no
value that could be read as an option.
"""

import os
import shutil
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from calc.structure import Structure
from calc.xtb_cli import CliError, _from_xyz, _safe, _to_xyz
from chemclaw.config import settings

# What to search for. Each is a different CREST run mode over the same machinery.
# Searches over **one** molecule. Separate from the union below because the agent-facing
# ensemble tool takes exactly these — a complex search needs a second molecule, so it is a
# different tool rather than a fifth option on this one.
EnsembleSearch = Literal["conformers", "tautomers", "protomers", "deprotomers"]
CrestSearch = Literal["conformers", "tautomers", "protomers", "deprotomers", "complex"]
_SEARCH_FLAGS: dict[CrestSearch, list[str]] = {
    "conformers": [],
    "tautomers": ["--tautomerize"],
    "protomers": ["--protonate"],
    "deprotomers": ["--deprotonate"],
    # Non-covalent mode: adds a logfermi wall potential around the pair, without which a
    # metadynamics search simply lets two molecules drift apart instead of sampling how
    # they bind (`calc.complexes`).
    "complex": ["--nci"],
}

# How hard to search. `quick` trades completeness for wall clock and is the right default
# for a screening question; `extensive` is for the case where a missed conformer changes
# the answer. CREST's own names, so the mapping stays legible against its documentation.
CrestEffort = Literal["quick", "normal", "extensive"]
_EFFORT_FLAGS: dict[CrestEffort, list[str]] = {
    "quick": ["--quick"],
    "normal": [],
    "extensive": ["--mrest", "10"],
}

_METHOD_FLAGS = {
    "GFN2-xTB": ["--gfn2"],
    "GFN1-xTB": ["--gfn1"],
    "GFN-FF": ["--gfnff"],
}

_ENV_ALLOWLIST = ("PATH", "HOME", "LANG", "LC_ALL")

# The file each search writes its ensemble to.
_ENSEMBLE_FILES: dict[CrestSearch, tuple[str, ...]] = {
    "conformers": ("crest_conformers.xyz",),
    "tautomers": ("tautomers.xyz", "crest_conformers.xyz"),
    "protomers": ("protomers.xyz", "crest_conformers.xyz"),
    "deprotomers": ("deprotonated.xyz", "crest_conformers.xyz"),
    "complex": ("crest_conformers.xyz",),
}


class EnsembleMember(BaseModel):
    """One structure of a CREST ensemble, with the energy it was ranked by.

    `degeneracy` is how many **rotamers** collapse onto this conformer — n-butane's
    gauche is two mirror-image rotamers, and its methyl rotations multiply further. It is
    not bookkeeping: a population that ignores it is simply wrong, and by a lot. Measured
    on n-butane, degeneracy-weighted populations give the anti 59.2% against CREST's own
    reported 59.14%; ignoring degeneracy gives 73%.
    """

    energy_hartree: float
    degeneracy: int = 1
    structure: Structure


@lru_cache(maxsize=1)
def binary_path() -> str | None:
    """Absolute path to the configured `crest` binary, or None when it is absent."""
    return shutil.which(settings.crest_binary)


def is_available() -> bool:
    """Whether ensemble sampling can run at all."""
    return binary_path() is not None


@lru_cache(maxsize=1)
def binary_version() -> str:
    """The installed CREST version, for the cache key (an upgrade must recompute)."""
    path = binary_path()
    if path is None:
        return "absent"
    output = subprocess.run(  # noqa: S603 — fixed argv, no shell, resolved path
        [path, "--version"], capture_output=True, text=True, timeout=60, check=False
    ).stdout
    for line in output.splitlines():
        if "version" in line.lower():
            words = [word.strip(",") for word in line.split()]
            for index, word in enumerate(words):
                if word.lower() == "version" and index + 1 < len(words):
                    return words[index + 1]
    return "unknown"


def _read_degeneracies(directory: Path, count: int) -> list[int]:
    """Rotamer counts per conformer from CREST's `cre_members`, or all ones if absent.

    Format: a count, then one line per conformer whose first field is how many rotamers
    it represents. Only the conformer search writes it; a tautomer or protomer ensemble
    has no rotamer grouping, and every member then weighs the same.
    """
    members = directory / "cre_members"
    if not members.exists():
        return [1] * count
    rows = [line.split() for line in members.read_text().splitlines() if line.split()]
    degeneracies = [int(row[0]) for row in rows[1:] if row[0].isdigit()]
    return degeneracies if len(degeneracies) == count else [1] * count


def _read_ensemble(path: Path, template: Structure) -> list[EnsembleMember]:
    """Parse a multi-structure XYZ; CREST writes the energy on each comment line.

    Charge and multiplicity come from the template rather than being re-derived, because
    for a conformer search they are invariant by construction — and for a protomer search
    the caller adjusts them explicitly, which is the only honest way to change an
    electron count.
    """
    lines = path.read_text().splitlines()
    members: list[EnsembleMember] = []
    cursor = 0
    while cursor < len(lines) and lines[cursor].strip():
        count = int(lines[cursor].split()[0])
        energy = float(lines[cursor + 1].split()[0])
        block = "\n".join(lines[cursor : cursor + 2 + count])
        members.append(
            EnsembleMember(energy_hartree=energy, structure=_from_xyz(block, template, None))
        )
        cursor += 2 + count
    return members


def run(
    structure: Structure,
    *,
    search: CrestSearch,
    method: str,
    effort: CrestEffort = "quick",
    solvent: str | None = None,
    temperature_k: float | None = None,
) -> list[EnsembleMember]:
    """Run one CREST search and return its ensemble, lowest energy first.

    Args:
        structure: The starting geometry, its charge and its multiplicity.
        search: Which space to sample.
        method: GFN parametrization; CREST accepts GFN1/GFN2 and GFN-FF.
        effort: How hard to search.
        solvent: ALPB implicit solvent name, or None for gas phase.
        temperature_k: Sampling temperature; None uses the configured default.

    Returns:
        The ensemble members ordered by energy.

    Raises:
        CliError: CREST is absent, timed out, exited non-zero, or wrote no ensemble.
        ValueError: the method is not one CREST accepts.
    """
    path = binary_path()
    if path is None:
        raise CliError(
            f"the {settings.crest_binary!r} binary is not installed: conformer, tautomer "
            "and protomer sampling are unavailable in this deployment"
        )
    if method not in _METHOD_FLAGS:
        raise ValueError(f"CREST does not support method {method!r}")

    argv = [path, "input.xyz", *_METHOD_FLAGS[method], *_SEARCH_FLAGS[search]]
    argv += [*_EFFORT_FLAGS[effort], "--chrg", str(structure.charge)]
    argv += ["--uhf", str(structure.uhf)]
    argv += ["--temp", str(temperature_k or settings.xtb_thermo_temperature_k)]
    if settings.crest_threads > 0:
        argv += ["-T", str(settings.crest_threads)]
    if solvent is not None:
        argv += ["--alpb", _safe(solvent, "solvent")]

    with tempfile.TemporaryDirectory(prefix="crest-") as workdir:
        directory = Path(workdir)
        (directory / "input.xyz").write_text(_to_xyz(structure))
        environment = _environment()
        try:
            completed = subprocess.run(  # noqa: S603 — fixed argv, no shell, resolved path
                argv,
                cwd=directory,
                env=environment,
                capture_output=True,
                text=True,
                timeout=settings.crest_timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise CliError(
                f"crest {search} timed out after {settings.crest_timeout_seconds}s; "
                "a larger molecule needs a longer budget or a cheaper effort level"
            ) from error
        if completed.returncode != 0:
            tail = "\n".join(completed.stdout.splitlines()[-12:])
            raise CliError(f"crest {search} failed (exit {completed.returncode}):\n{tail}")
        for name in _ENSEMBLE_FILES[search]:
            candidate = directory / name
            if candidate.exists():
                members = _read_ensemble(candidate, structure)
                degeneracies = _read_degeneracies(directory, len(members))
                return [
                    member.model_copy(update={"degeneracy": degeneracy})
                    for member, degeneracy in zip(members, degeneracies, strict=True)
                ]
        raise CliError(f"crest {search} wrote no ensemble file")


def _environment() -> dict[str, str]:
    """The scrubbed environment the child runs in."""
    environment = {key: os.environ[key] for key in _ENV_ALLOWLIST if key in os.environ}
    if settings.crest_threads > 0:
        environment["OMP_NUM_THREADS"] = str(settings.crest_threads)
    return environment
