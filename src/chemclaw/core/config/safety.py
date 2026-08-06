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
    # The most components one reaction screen may carry. A reaction screen checks every
    # incompatible-pair rule as a cross-product of the components that match each side, so the
    # flags it can produce grow with the *square* of the input while the request stays tiny:
    # 13 KiB of SMILES was measured producing 251,000 flags and blocking the connector's event
    # loop for 2.48 s. `connector_max_request_bytes` is no bound on that, because the
    # amplification is in the response.
    #
    # 64 is far above any real reaction — the largest shipped ELN entry has well under a dozen
    # species — and bounds the worst case to ~1,000 pair flags and single-digit milliseconds.
    # Refused rather than truncated: a hazard screen that silently dropped components would
    # report "no rule matched" for chemistry it never looked at, which is the one failure this
    # tool's own description says must never happen.
    safety_max_components: int = Field(default=64, gt=0)
