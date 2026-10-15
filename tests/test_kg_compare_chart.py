"""The comparison's alerts, driven; and the pause the chart derives from the sync interval.

The rules are rendered by Helm and run through `promtool test rules`, in both directions, because a
rule nobody has watched fire is a claim that an alert exists. Skipped where helm or promtool is not
installed (the skip names which). The settings tests need neither.
"""

import shutil
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from chemclaw.core.config import Settings

_CHART = Path(__file__).resolve().parents[1] / "deploy" / "helm" / "chemclaw"
_DIFFER = "ChemclawKnowledgeStoresDiffer"
_NOT_RUNNING = "ChemclawKnowledgeCompareNotRunning"
_DIFFERENCES = "chemclaw_kg_compare_differences"
_STAMP = "chemclaw_kg_compare_last_run_timestamp_seconds"

needs_tools = pytest.mark.skipif(
    shutil.which("helm") is None or shutil.which("promtool") is None,
    reason="helm is not installed (or promtool is): both are needed to evaluate a rendered rule",
)
needs_helm = pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")


def _helm(*extra: str) -> list[dict[str, Any]]:
    """Every document of the chart rendered with the postures every caller states, plus `extra`."""
    rendered = subprocess.run(
        [
            "helm",
            "template",
            "chemclaw",
            str(_CHART),
            "--set",
            "networkPolicy.allowAnyDestination=true",
            "--set",
            "retention.unboundedGrowthAccepted=true",
            "--set",
            "temporal.namespace=chemclaw",
            "--set",
            "allowNonSemanticEmbeddings=true",
            *extra,
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [d for d in yaml.safe_load_all(rendered) if isinstance(d, dict)]


def _rules(*extra: str) -> dict[str, dict[str, Any]]:
    """The alert rules of the render, by name."""
    found: dict[str, dict[str, Any]] = {}
    for document in _helm(*extra):
        if document.get("kind") == "PrometheusRule":
            for group in document["spec"]["groups"]:
                for rule in group["rules"]:
                    if "alert" in rule:
                        found[rule["alert"]] = rule
    return found


def _drive(tmp_path: Path, rule: dict[str, Any], tests: list[dict[str, Any]]) -> None:
    """Run `promtool test rules` over `rule` with `tests`; fail with its output if any disagrees."""
    (tmp_path / "rules.yaml").write_text(
        yaml.safe_dump({"groups": [{"name": "kg", "rules": [rule]}]})
    )
    (tmp_path / "unit.yaml").write_text(
        yaml.safe_dump({"rule_files": ["rules.yaml"], "evaluation_interval": "1h", "tests": tests})
    )
    driven = subprocess.run(
        ["promtool", "test", "rules", "unit.yaml"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert driven.returncode == 0, f"{driven.stdout}\n{driven.stderr}"


def _firing(rule: dict[str, Any], **labels: str) -> dict[str, Any]:
    return {
        "exp_labels": {"alertname": rule["alert"], **rule["labels"], **labels},
        "exp_annotations": rule["annotations"],
    }


def _case(series: list[tuple[str, str]], at: str, alert: str, firing: list[dict[str, Any]]) -> Any:
    return {
        "interval": "1h",
        "input_series": [{"series": name, "values": values} for name, values in series],
        "alert_rule_test": [{"eval_time": at, "alertname": alert, "exp_alerts": firing}],
    }


@needs_tools
def test_the_differences_alert_follows_the_replica_that_compared_last(tmp_path: Path) -> None:
    """Night 1 on pod a found three; night 2 on pod b found none: a's old gauge must not page.

    A gauge keeps its value until its process dies, so a `max()` over the family kept a's finding
    alive for as long as a lived and the seven clean days could never complete.
    """
    rule = _rules("--set", "config.CHEMCLAW_KG_STORE=dual")[_DIFFER]
    a = f'{_DIFFERENCES}{{kind="canonical",pod="a"}}'
    b = f'{_DIFFERENCES}{{kind="canonical",pod="b"}}'
    a_run, b_run = f'{_STAMP}{{pod="a"}}', f'{_STAMP}{{pod="b"}}'
    night_one, night_two = "3600", "90000"
    _drive(
        tmp_path,
        rule,
        [
            # b compared later and found nothing: silent, though a still serves its three.
            _case(
                [(a, "3x48"), (a_run, f"{night_one}x48"), (b, "0x48"), (b_run, f"{night_two}x48")],
                "30h",
                _DIFFER,
                [],
            ),
            # b compared later and found one: fires, naming b.
            _case(
                [(a, "3x48"), (a_run, f"{night_one}x48"), (b, "1x48"), (b_run, f"{night_two}x48")],
                "30h",
                _DIFFER,
                [_firing(rule, pod="b")],
            ),
            # a compared last and found three: fires, naming a, whatever b still says.
            _case(
                [(a, "3x48"), (a_run, f"{night_two}x48"), (b, "0x48"), (b_run, f"{night_one}x48")],
                "30h",
                _DIFFER,
                [_firing(rule, pod="a")],
            ),
        ],
    )


@needs_tools
def test_a_comparison_that_stops_completing_raises_an_alert_after_the_hold(
    tmp_path: Path,
) -> None:
    """Silent while runs complete; firing once none has for two intervals and the hold has passed.

    The schedule is daily, so the window is two days and the hold one. The three ways a comparison
    can fail to complete without any difference being reported: it stopped, it never ran (the
    gauge is 0), and nothing reports the family at all.
    """
    rule = _rules("--set", "config.CHEMCLAW_KG_STORE=dual")[_NOT_RUNNING]
    assert "172800" in str(rule["expr"]) and rule["for"] == "1440m"
    stamp = f'{_STAMP}{{pod="a"}}'
    day = 86400
    _drive(
        tmp_path,
        rule,
        [
            # Every scrape finds a fresh completion: never fires, however long it is left.
            _case([(stamp, "0+3600x240")], "8d", _NOT_RUNNING, []),
            # Two completions, then nothing: silent through the window and the hold, then firing.
            _case([(stamp, f"{2 * day}x240")], "4d", _NOT_RUNNING, []),
            _case([(stamp, f"{2 * day}x240")], "5d12h", _NOT_RUNNING, [_firing(rule)]),
            # Never completed (the gauge reads 0): a fresh install is quiet through the hold.
            _case([(stamp, "0x240")], "2d12h", _NOT_RUNNING, []),
            _case([(stamp, "0x240")], "4d", _NOT_RUNNING, [_firing(rule)]),
            # Nothing reports the family: the same hold, then firing.
            _case([('unrelated{pod="a"}', "1x240")], "12h", _NOT_RUNNING, []),
            _case([('unrelated{pod="a"}', "1x240")], "3d", _NOT_RUNNING, [_firing(rule)]),
        ],
    )


@needs_helm
def test_the_not_running_alert_exists_only_where_there_is_a_comparison_to_miss() -> None:
    assert _NOT_RUNNING not in _rules()
    assert _NOT_RUNNING in _rules("--set", "config.CHEMCLAW_KG_STORE=dual")
    schedule = _rules(
        "--set",
        "config.CHEMCLAW_KG_STORE=dual",
        "--set",
        "config.CHEMCLAW_KG_COMPARE_SCHEDULE_MINUTES=60",
    )[_NOT_RUNNING]
    assert "7200" in str(schedule["expr"]) and schedule["for"] == "60m"


def _config(*extra: str) -> dict[str, str]:
    for document in _helm(*extra):
        data = document.get("data")
        if document.get("kind") == "ConfigMap" and isinstance(data, dict):
            if "CHEMCLAW_KNOWLEDGE_DIR" in data:
                return cast(dict[str, str], data)
    raise AssertionError("no ConfigMap carries the application's settings")


@needs_helm
def test_the_chart_derives_the_comparison_pause_from_the_sync_interval() -> None:
    """Two sync intervals and a margin: a clone's lag cannot outlast the pause."""
    default = _config("--set", "config.CHEMCLAW_KG_STORE=dual")
    assert (
        default["CHEMCLAW_KG_COMPARE_SETTLE_SECONDS"],
        default["CHEMCLAW_KG_COMPARE_TIMEOUT_SECONDS"],
    ) == (
        "720",
        "1200",
    )
    slower = _config(
        "--set", "config.CHEMCLAW_KG_STORE=dual", "--set", "knowledge.sync.intervalSeconds=600"
    )
    assert slower["CHEMCLAW_KG_COMPARE_SETTLE_SECONDS"] == "1320"
    assert slower["CHEMCLAW_KG_COMPARE_TIMEOUT_SECONDS"] == "1800"
    stated = _config(
        "--set",
        "config.CHEMCLAW_KG_STORE=dual",
        "--set",
        "config.CHEMCLAW_KG_COMPARE_SETTLE_SECONDS=100",
    )
    assert stated["CHEMCLAW_KG_COMPARE_SETTLE_SECONDS"] == "100"
    assert stated["CHEMCLAW_KG_COMPARE_TIMEOUT_SECONDS"] == "580"
    assert "CHEMCLAW_KG_COMPARE_SETTLE_SECONDS" not in _config()


def test_the_code_default_is_what_the_chart_derives_for_its_own_default_interval() -> None:
    values = yaml.safe_load((_CHART / "values.yaml").read_text())
    interval = int(values["knowledge"]["sync"]["intervalSeconds"])
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.kg_compare_settle_seconds == 2 * interval + 120
    assert settings.kg_compare_timeout_seconds == settings.kg_compare_settle_seconds + 480


def test_the_comparison_budget_must_outlast_its_pause() -> None:
    with pytest.raises(ValueError, match="does not outlast"):
        Settings(  # type: ignore[call-arg]
            _env_file=None, kg_compare_settle_seconds=900, kg_compare_timeout_seconds=900
        )
