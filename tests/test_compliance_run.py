"""The compliance suite's run half, driven through `make live-compliance`'s own entry point.

The front door is a loopback stand-in that answers each turn from the real preference store — what
`StandingPreferences` does on every model call — and records its sessions in the real ownership
table. So these tests can see what no unit test of the scorer can: that the seed is in force for
exactly the preference arm and gone afterwards, that it is written under the actor the turns run
as, that a control arm is refused when something else already prohibits the species, and that a
path whose control never recommends it is reported blind.
"""

from __future__ import annotations

import asyncio
import functools
import itertools
import json
import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from chemclaw.agent.preferences import PreferenceStore
from chemclaw.agent.session_store import SessionOwnerStore
from chemclaw.cli import live_probes
from chemclaw.core.config import settings
from chemclaw.evals import compliance_run
from chemclaw.evals.compliance import MINIMUM_REPEATS, PATH_PROBES, PROHIBITION
from chemclaw.evals.live import load_probes
from tests.pg import migrated_db_or_skip

PROBE_DIR = Path(__file__).resolve().parents[1] / "data" / "evals" / "probes"

#: What the stand-in says with the prohibition on file, and without it: the shapes the issue's
#: compliant and violating runs took.
_COMPLIANT = "Use DMSO with K2CO3 at 80 °C. DMF is excluded on this project."
_TEXTBOOK = "Typical conditions: K2CO3 in DMF or DMSO at 80 °C."
_HABIT = "Use DMSO with K2CO3 at 80 °C."


@dataclass
class _FrontDoor:
    """A loopback front door whose answers depend on what the preference store holds for `owner`.

    `textbook` is what it says with nothing on file; `seen` records, per turn, whether the seed was
    on file when the turn was asked, in arrival order. The protocol probe's turn also writes a
    design receipt and serves the design, so the suite's design read is exercised.
    """

    owner: str
    textbook: str = _TEXTBOOK
    seen: list[bool] = field(default_factory=list)
    sessions: Iterator[int] = field(default_factory=itertools.count)
    designs: dict[tuple[str, int], dict[str, object]] = field(default_factory=dict)
    revisions: Iterator[int] = field(default_factory=lambda: itertools.count(1))
    lock: threading.Lock = field(default_factory=threading.Lock)

    def answer(self, question: str) -> list[dict[str, object]]:
        """The events of one turn, decided by the store as the turn is asked."""
        held = asyncio.run(PreferenceStore().recall(self.owner))
        seeded = any(p.key == PROHIBITION.key for p in held)
        with self.lock:
            self.seen.append(seeded)
            revision = next(self.revisions)
        text = _COMPLIANT if seeded else self.textbook
        events: list[dict[str, object]] = []
        if question == _questions()["protocol"]:
            design_id = "design-00000000c0de"
            solvent = "DMSO" if seeded else "DMF"
            self.designs[(design_id, revision)] = {
                "request": {"forbidden": ["DMF"] if seeded else [], "components": []},
                "base": {"setpoints": {"solvent": solvent}},
            }
            receipt = json.dumps(
                {"design_id": design_id, "revision": revision, "title": "Cyanation"},
                separators=(",", ":"),
            )
            events.append(
                {"type": "tool_result", "tool": "draft_experiment_protocol", "preview": receipt}
            )
        events.append({"type": "answer", "text": text})
        return events


@functools.cache
def _questions() -> dict[str, str]:
    """Each path's question, from the shipped corpus the suite reads."""
    probes = compliance_run.path_probes(load_probes(str(PROBE_DIR)))
    return {path: probe.question for path, probe in probes.items()}


def _serve(front: _FrontDoor) -> ThreadingHTTPServer:
    """Start `front` on a loopback port; the caller shuts it down."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            if self.path == "/sessions":
                session_id = f"cmp-{uuid.uuid4().hex[:8]}-{next(front.sessions)}"
                asyncio.run(SessionOwnerStore().record(session_id, front.owner))
                self._send(200, json.dumps({"session_id": session_id}).encode(), "application/json")
                return
            events = front.answer(str(payload["message"]))
            body = "".join(f"data: {json.dumps(event)}\n\n" for event in events).encode()
            self._send(200, body, "text/event-stream")

        def do_DELETE(self) -> None:
            self._send(204, b"", "application/json")

        def do_GET(self) -> None:
            url = urlparse(self.path)
            revision = int(parse_qs(url.query)["revision"][0])
            design = front.designs[(url.path.rsplit("/", 1)[1], revision)]
            self._send(200, json.dumps({"design": design}).encode(), "application/json")

        def log_message(self, *args: object) -> None:
            """Silent; `front.seen` is the record."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def lane(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A durable store, a real gateway's name, and a transcript directory of the test's own."""
    asyncio.run(migrated_db_or_skip())
    monkeypatch.setattr(settings, "session_store", "postgres")
    monkeypatch.setattr(settings, "llm_base_url", "https://gateway.example/v1")
    return tmp_path


def _run(front: _FrontDoor, directory: Path, repeats: int = MINIMUM_REPEATS) -> int:
    server = _serve(front)
    try:
        return live_probes.main(
            [
                "--suite",
                "compliance",
                "--repeats",
                str(repeats),
                "--base-url",
                f"http://127.0.0.1:{server.server_address[1]}",
                "--transcript-dir",
                str(directory),
            ]
        )
    finally:
        server.shutdown()
        server.server_close()


def _held(owner: str) -> list[tuple[str, str]]:
    return [(p.key, p.value) for p in asyncio.run(PreferenceStore().recall(owner))]


def _records(directory: Path) -> list[dict[str, Any]]:
    evidence = json.loads((directory / "evidence.json").read_text(encoding="utf-8"))
    records: list[dict[str, Any]] = evidence["records"]
    return records


def test_the_seed_is_in_force_for_the_preference_arm_only_and_gone_afterwards(lane: Path) -> None:
    """Control first with nothing on file, then the seed, then nothing again — read by the turns."""
    front = _FrontDoor(owner=f"chemist-{uuid.uuid4().hex[:8]}")
    assert _run(front, lane) == 0
    per_arm = MINIMUM_REPEATS * len(PATH_PROBES)
    assert front.seen == [False] * per_arm + [True] * per_arm, (
        "the turns did not see the seed exactly during the preference arm"
    )
    assert _held(front.owner) == [], "the seed outlived the run"

    records = _records(lane)
    verdicts: dict[str, set[str]] = {"control": set(), "preference": set()}
    for record in records:
        verdicts[record["arm"]].add(record["score"]["verdict"])
    assert verdicts == {"control": {"fail"}, "preference": {"pass"}}, verdicts
    assert len(records) == 2 * per_arm

    protocol = [r for r in records if r["path"] == "protocol"]
    assert all(r["designs"] for r in protocol), "the protocol path never read a stored design"
    assert sum(1 for r in protocol if r["reused"]) == len(protocol) - 1, (
        "a design every repeat revised must be named on every repeat after the first"
    )
    report = (lane / "summary.md").read_text(encoding="utf-8")
    assert (
        f"| direct | preference | {MINIMUM_REPEATS} | {MINIMUM_REPEATS}/{MINIMUM_REPEATS} |"
        in report
    )


def test_a_control_arm_is_refused_when_another_preference_already_prohibits_it(lane: Path) -> None:
    """Somebody's own constraint makes the control a second treatment; it is not deleted either."""
    owner = f"chemist-{uuid.uuid4().hex[:8]}"
    asyncio.run(PreferenceStore().remember(owner, "solvent_policy", "no DMF anywhere on site"))
    front = _FrontDoor(owner=owner)
    assert _run(front, lane) == 2
    assert front.seen == [], "a turn was paid for on an arm that was refused"
    assert _held(owner) == [("solvent_policy", "no DMF anywhere on site")]


def test_a_seed_an_earlier_run_left_behind_is_removed_before_the_control_arm(lane: Path) -> None:
    """Only this suite's exact seed is treated as its own."""
    owner = f"chemist-{uuid.uuid4().hex[:8]}"
    asyncio.run(PreferenceStore().remember(owner, PROHIBITION.key, PROHIBITION.value))
    asyncio.run(PreferenceStore().remember(owner, "project", "PRJ-9"))
    asyncio.run(compliance_run.clear_for_control(owner))
    assert _held(owner) == [("project", "PRJ-9")]


def test_a_path_whose_control_never_recommends_the_species_is_reported_blind(lane: Path) -> None:
    """A model that avoids it unasked passes in both arms, and the run must say it measured that."""
    front = _FrontDoor(owner=f"chemist-{uuid.uuid4().hex[:8]}", textbook=_HABIT)
    assert _run(front, lane) == 2
    report = (lane / "summary.md").read_text(encoding="utf-8")
    assert "**blind on direct, deep-research**" in report, report


def test_a_run_below_the_floor_reports_but_exits_as_a_reading(lane: Path) -> None:
    """Fewer repeats than the floor still writes everything, and says it is not a rate."""
    front = _FrontDoor(owner=f"chemist-{uuid.uuid4().hex[:8]}")
    assert _run(front, lane, repeats=1) == 2
    assert "below the floor" in (lane / "summary.md").read_text(encoding="utf-8")
    assert _held(front.owner) == []


def test_a_runner_on_the_in_memory_store_is_refused_before_anything_is_asked(
    lane: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A seed in this process's memory would never reach the front door's store."""
    monkeypatch.setattr(settings, "session_store", "memory")
    front = _FrontDoor(owner=f"chemist-{uuid.uuid4().hex[:8]}")
    assert _run(front, lane) == 2
    assert front.seen == []
