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
_PROCESSES = REPO_ROOT / "infra/live/processes.sh"


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
    """With no key in the environment the script stops before it mints a token or starts a process.

    Nothing reaches the gateway's directory: no token, no pidfile.
    """
    result = _run(["up"], tmp_path)
    assert result.returncode != 0
    assert "no vendor key" in result.stderr
    gateway_dir = tmp_path / "gateway"
    assert not (gateway_dir / "gateway.token").exists()
    assert not (gateway_dir / "gateway.pid").exists()


def test_env_refuses_before_up_and_never_prints_the_vendor_key(tmp_path: Path) -> None:
    """`env` without a minted token fails; with one, it exports the token, not the key."""
    secret = "sk-ant-test-never-printed"
    assert _run(["env"], tmp_path, ANTHROPIC_API_KEY=secret).returncode != 0

    (tmp_path / "gateway").mkdir()
    (tmp_path / "gateway" / "gateway.token").write_text("sk-lane-local\n", encoding="utf-8")
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


def test_stopping_the_lane_leaves_the_gateway_and_its_token(tmp_path: Path) -> None:
    """`processes.sh down` stops the lane's processes, not the gateway that a restart cannot start.

    The gateway needs the vendor key to start, which a shell restarting the lane need not hold.
    """
    lane = subprocess.Popen(["sleep", "60"])
    gateway = subprocess.Popen(["sleep", "60"])
    try:
        (tmp_path / "run").mkdir()
        (tmp_path / "run" / "api.pid").write_text(f"{lane.pid}\n", encoding="utf-8")
        (tmp_path / "gateway").mkdir()
        (tmp_path / "gateway" / "gateway.pid").write_text(f"{gateway.pid}\n", encoding="utf-8")
        (tmp_path / "gateway" / "gateway.token").write_text("sk-lane-local\n", encoding="utf-8")
        env = {"PATH": os.environ["PATH"], "CHEMCLAW_LIVE_DIR": str(tmp_path)}
        result = subprocess.run(
            ["bash", str(_PROCESSES), "down"], env=env, capture_output=True, text=True, timeout=30
        )
        assert result.returncode == 0, result.stderr
        assert lane.wait(timeout=10) is not None, "the lane's own process was not stopped"
        assert gateway.poll() is None, "stopping the lane stopped the gateway"
        assert (tmp_path / "gateway" / "gateway.token").exists()
    finally:
        for proc in (lane, gateway):
            if proc.poll() is None:
                proc.kill()
                proc.wait()
