"""Structural hazard screening of proposed chemistry (D-080).

One domain section of the composed ChemClaw `Settings`. The package `__init__.py` flattens
every section into the one config object and owns the env prefix, the `.env` loading and the
cross-section validators; fields, env names and defaults are exactly as they were when all
sections shared a single module (D-072 mixins, split per D-156).
"""

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings

from chemclaw.core.config.shipped import _shipped


class SafetySettings(BaseSettings):
    """Structural hazard screening of proposed chemistry (D-080).

    Grouped because both knobs govern one advisory gate: which rule table is screened against,
    and how serious a flag must be before a proposed procedure note is required to document it.
    """

    # The committed, cited SMARTS rule table (`science/safety/screen.py`). A path, not inline
    # rules: a process-safety chemist maintains it as data, and a deployment can point at its own
    # table.
    safety_rules_path: str = Field(
        default_factory=lambda: _shipped("science", "safety", "rules.yaml")
    )
    # The minimum flag severity that makes a `## Hazards` section mandatory in an agent-proposed
    # procedure note (enforced by `kg-validate`, so it gates the PR rather than the runtime).
    # "high" only, by default: the gate must fire rarely enough that a firing means something.
    safety_gate_severity: Literal["high", "medium", "low"] = "high"
    # Whether `kg-validate` enforces that gate at all. On by default — the corpus holds no
    # procedure notes yet, so it costs nothing today and is the conservative direction for a
    # safety check; a deployment migrating a legacy corpus can turn it off while it catches up.
    safety_gate_enabled: bool = True
