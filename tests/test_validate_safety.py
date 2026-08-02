"""The safety-table validator catches a bad rule/alert table (Science-5, `make safety-validate`).

Both `science/safety/screen.py::_load_rules` and `science/safety/genotox.py::_load_alerts` are
lazy (`@lru_cache`, first-request compile): a malformed YAML or an unparseable SMARTS is fatal but
only surfaces on the first real screen. This is the gate that forces the compile up front, proven
here the same way `test_validate_skills.py` proves its own gate: the shipped tables pass, and each
half's failure mode is independently reported rather than only crashing the whole check.
"""

from pathlib import Path

import pytest

from chemclaw.cli.validate_safety import validate_safety
from chemclaw.core.config import settings
from chemclaw.science.safety import genotox


def test_the_shipped_tables_compile_cleanly() -> None:
    """The committed rule and alert tables must already pass — the gate's baseline."""
    assert validate_safety() == []


def test_a_broken_process_safety_rule_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unparseable SMARTS in `rules.yaml` is caught here, not on the first live screen."""
    table = tmp_path / "rules.yaml"
    table.write_text(
        "structural:\n"
        "  - id: broken-rule\n"
        '    smarts: "[not-a-smarts"\n'
        "    severity: high\n"
        "    explanation: x\n"
        "    citation: y\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "safety_rules_path", str(table))

    problems = validate_safety()

    assert any("broken-rule" in problem for problem in problems)
    assert any("process-safety" in problem for problem in problems)


def test_a_broken_genotox_alert_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second table has its own loader and its own cache; both must be exercised, not just one.

    `_load_alerts` is cached with no arguments (one alert set, unlike the per-path rule table), so
    the cache is cleared before pointing it at the broken fixture and again afterwards — otherwise
    this test would either read a stale real table or leave a broken one cached for every test that
    runs after it.
    """
    alerts = tmp_path / "genotox_alerts.yaml"
    alerts.write_text(
        "structural:\n"
        "  - id: broken-alert\n"
        '    smarts: "[not-a-smarts"\n'
        "    motif: x\n"
        "    explanation: x\n"
        "    citation: y\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(genotox, "_ALERTS_PATH", str(alerts))
    genotox._load_alerts.cache_clear()
    try:
        problems = validate_safety()
        assert any("broken-alert" in problem for problem in problems)
        assert any("genotoxicity" in problem for problem in problems)
    finally:
        genotox._load_alerts.cache_clear()
