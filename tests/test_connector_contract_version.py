"""A connector whose server was built against another MAJOR of its contract is refused by name.

The manifest (the fleet's `chemclaw-contracts`) declares a `contract_version`; the server reports
its own on `/healthz`. At session open a major difference refuses that connector through the
existing degraded path (so the chemist is told, before the first token, which capability is
missing), a minor one is logged, and a value missing on either side is unknown and never refuses.
A stub server answers `/healthz` with whatever the test says and records what reached `/mcp`.
"""

import asyncio
import logging
from contextlib import AsyncExitStack
from typing import Any

import pytest
from fastapi import Request
from fastapi.responses import JSONResponse
from mcp.server.fastmcp import FastMCP

from chemclaw.connectors.contract import compare
from chemclaw.connectors.manifest import ConnectorManifest
from chemclaw.connectors.registry import _mcp_connection, _with_contract, open_connector_specs
from chemclaw.connectors.server import connector_app
from tests.conftest import _free_port
from tests.test_capability_degradation import _stream_events
from tests.test_connector_transport import _Server

_NAME = "stub"


class _Stub:
    """A connector server whose `/healthz` body the test chooses, and which counts its calls."""

    def __init__(self, healthz: dict[str, Any] | None) -> None:
        self.healthz = healthz
        self.paths: list[str] = []
        server = FastMCP(_NAME)

        @server.tool()
        async def echo() -> str:
            """A trivial tool, so that an open has something to list."""
            return "ok"

        self.app = connector_app(server, name=_NAME)

        @self.app.middleware("http")
        async def _answer(request: Request, call_next: Any) -> Any:
            self.paths.append(request.url.path)
            if request.url.path == "/healthz":
                return JSONResponse(self.healthz if self.healthz is not None else {})
            return await call_next(request)

    def manifest(self, port: int, declared: str | None) -> ConnectorManifest:
        """The manifest the fleet would ship for this server, with `declared` as its version."""
        document: dict[str, Any] = {
            "name": _NAME,
            "description": "a stub capability",
            "endpoint": {
                "transport": "http",
                "url": f"http://127.0.0.1:{port}/mcp",
                "health_url": f"http://127.0.0.1:{port}/healthz",
                "tools": ["echo"],
                "read_only": ["echo"],
            },
        }
        if declared is not None:
            document["contract_version"] = declared
        return ConnectorManifest.model_validate(document)


def _open(manifest: ConnectorManifest) -> tuple[list[str], list[str]]:
    """Open the connector the way a turn does; return the tool names and the unreachable names."""

    async def _run() -> tuple[list[str], list[str]]:
        assert manifest.endpoint is not None
        spec = _with_contract(_mcp_connection(manifest, manifest.endpoint), manifest)
        async with AsyncExitStack() as stack:
            tools, unreachable = await open_connector_specs(stack, [spec])
            return [tool.name for tool in tools], unreachable

    return asyncio.run(_run())


@pytest.mark.parametrize(
    ("declared", "served", "expected"),
    [
        ("1.2.3", "1.2.3", "same"),
        ("1.2.3", "1.2.9", "same"),
        ("1.2.3", "1.3.0", "minor"),
        ("1.2.3", "1.1.0", "minor"),
        ("1.2.3", "2.0.0", "major"),
        ("2.0.0", "1.9.9", "major"),
        ("1.2.3", None, "unknown"),
        (None, "1.2.3", "unknown"),
        (None, None, "unknown"),
        ("1.2.3", "next", "unknown"),
    ],
)
def test_the_comparison_reads_major_then_minor(
    declared: str | None, served: str | None, expected: str
) -> None:
    """MAJOR decides refusal, MINOR decides a warning, a patch decides nothing, junk is unknown."""
    assert compare(declared, served) == expected


def test_a_major_mismatch_refuses_the_connector_by_name_without_dialling_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The server is asked on `/healthz` and never on `/mcp`; the name comes back unreachable."""
    stub = _Stub({"contract_version": "2.0.0"})
    port = _free_port()
    with _Server(stub.app, port), caplog.at_level(logging.WARNING):
        tools, unreachable = _open(stub.manifest(port, "1.4.0"))

    assert tools == []
    assert unreachable == [_NAME]
    assert "/healthz" in stub.paths
    assert not any(path.startswith("/mcp") for path in stub.paths), stub.paths
    refusal = " ".join(record.getMessage() for record in caplog.records)
    assert "connector stub is refused" in refusal
    assert "1.4.0" in refusal
    assert "2.0.0" in refusal


def test_a_major_mismatch_reaches_the_chemist_as_capability_degraded() -> None:
    """The refusal rides the existing event: named, and before the first token."""
    stub = _Stub({"contract_version": "2.0.0"})
    port = _free_port()
    manifest = stub.manifest(port, "1.4.0")
    assert manifest.endpoint is not None
    spec = _with_contract(_mcp_connection(manifest, manifest.endpoint), manifest)
    with _Server(stub.app, port):
        events = _stream_events([spec])

    assert [event["type"] for event in events][:2] == ["capability_degraded", "token"]
    assert events[0]["connectors"] == [_NAME]


def test_a_minor_mismatch_is_used_and_warned_about_once(caplog: pytest.LogCaptureFixture) -> None:
    """An additive difference keeps the connector; the warning is not repeated every turn."""
    stub = _Stub({"contract_version": "1.5.0"})
    port = _free_port()
    with _Server(stub.app, port), caplog.at_level(logging.WARNING):
        first = _open(stub.manifest(port, "1.4.0"))
        second = _open(stub.manifest(port, "1.4.0"))

    assert first == (["echo"], []) == second
    warned = [record for record in caplog.records if "additively" in record.getMessage()]
    assert len(warned) == 1, [record.getMessage() for record in caplog.records]
    assert "1.4.0" in warned[0].getMessage()
    assert "1.5.0" in warned[0].getMessage()


def test_an_agreeing_server_is_used_silently(caplog: pytest.LogCaptureFixture) -> None:
    """Same major and minor, a different patch: nothing to say."""
    stub = _Stub({"contract_version": "1.4.7"})
    port = _free_port()
    with _Server(stub.app, port), caplog.at_level(logging.INFO, logger="chemclaw.connectors"):
        assert _open(stub.manifest(port, "1.4.0")) == (["echo"], [])
    assert not [r for r in caplog.records if "contract" in r.getMessage()]


def test_a_server_that_reports_no_version_is_used_and_logged_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Unknown on the server's side is no refusal; the log line is written on the first open."""
    stub = _Stub({"status": "ok"})
    port = _free_port()
    with _Server(stub.app, port), caplog.at_level(logging.INFO, logger="chemclaw.connectors"):
        assert _open(stub.manifest(port, "1.4.0")) == (["echo"], [])
        assert _open(stub.manifest(port, "1.4.0")) == (["echo"], [])

    unknown = [r for r in caplog.records if "contract version unknown" in r.getMessage()]
    assert len(unknown) == 1, [record.getMessage() for record in caplog.records]


def test_a_manifest_that_declares_no_version_is_used_without_asking_the_server(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Unknown on the manifest's side: no `/healthz` round trip, one line, no refusal."""
    stub = _Stub({"contract_version": "9.9.9"})
    port = _free_port()
    with _Server(stub.app, port), caplog.at_level(logging.INFO, logger="chemclaw.connectors"):
        assert _open(stub.manifest(port, None)) == (["echo"], [])
        assert _open(stub.manifest(port, None)) == (["echo"], [])

    assert "/healthz" not in stub.paths
    unknown = [r for r in caplog.records if "contract version unknown" in r.getMessage()]
    assert len(unknown) == 1, [record.getMessage() for record in caplog.records]
