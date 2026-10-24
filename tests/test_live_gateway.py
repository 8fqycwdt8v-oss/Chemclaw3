"""The live lane's caching gateway keeps the vendor key off disk and caches where a turn reuses.

`infra/live/gateway/up.sh` is the one process that sees the vendor key; the lane gets a local token
instead. These tests drive the script itself (no gateway is started), and read the config the
gateway runs.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml

from tests.siblings import REPO_ROOT

_UP = REPO_ROOT / "infra/live/gateway/up.sh"
_CONFIG = REPO_ROOT / "infra/live/gateway/litellm.yaml"


def _run(args: list[str], live_dir: Path, **extra: str) -> subprocess.CompletedProcess[str]:
    """Run `up.sh` with a clean environment: no vendor key unless `extra` names one."""
    env = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ.get("HOME", ""),
        "CHEMCLAW_LIVE_DIR": str(live_dir),
    }
    env.update(extra)
    return subprocess.run(
        ["bash", str(_UP), *args], env=env, capture_output=True, text=True, timeout=30
    )


def test_up_refuses_without_a_vendor_key_and_writes_nothing(tmp_path: Path) -> None:
    """With no key in the environment the script stops before it mints a token or starts anything."""
    result = _run(["up"], tmp_path)
    assert result.returncode != 0
    assert "no vendor key" in result.stderr
    run_dir = tmp_path / "run"
    assert not (run_dir / "gateway.token").exists()
    assert not (run_dir / "gateway.pid").exists()


def test_env_refuses_before_up_and_never_prints_the_vendor_key(tmp_path: Path) -> None:
    """`env` without a minted token fails; with one, it exports the token, not the key."""
    secret = "sk-ant-test-never-printed"
    assert _run(["env"], tmp_path, ANTHROPIC_API_KEY=secret).returncode != 0

    (tmp_path / "run").mkdir()
    (tmp_path / "run" / "gateway.token").write_text("sk-lane-local\n", encoding="utf-8")
    result = _run(["env"], tmp_path, ANTHROPIC_API_KEY=secret)
    assert result.returncode == 0, result.stderr
    assert "export CHEMCLAW_LLM_API_KEY=sk-lane-local" in result.stdout
    assert "export CHEMCLAW_LLM_BASE_URL=http://127.0.0.1:4000/v1" in result.stdout
    assert secret not in result.stdout + result.stderr


def test_the_gateway_caches_the_prefix_and_the_turn_history() -> None:
    """Breakpoints sit on the system message (tools + system) and the last message (the turn)."""
    config = yaml.safe_load(_CONFIG.read_text(encoding="utf-8"))
    [route] = config["model_list"]
    points = route["litellm_params"]["cache_control_injection_points"]
    assert {"location": "message", "role": "system"} in points
    assert {"location": "message", "index": -1} in points
    assert len(points) <= 4, "Anthropic accepts at most four cache breakpoints per request"
    assert route["litellm_params"]["api_key"] == "os.environ/ANTHROPIC_API_KEY"


def test_the_gateway_version_is_pinned_exactly() -> None:
    """The process that holds the key runs one reviewed release, never whatever resolves today."""
    text = _UP.read_text(encoding="utf-8")
    assert 'readonly LITELLM_SPEC="litellm[proxy]==' in text
