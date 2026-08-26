"""Discovery, enablement and what the agent ends up advertising.

The registry is where folders on disk become agent capability, so these tests are about the two
things that decide that: which bundles are *found*, and which of those a deployment *enables*.
The
distinction is the whole point of the seam (a repo ships every connector; a deployment runs the
subset it has validated), and it is exactly where a silent failure would be most expensive — an
enabled name that resolves to nothing looks like a capability that quietly stopped working.

Bundles are written to `tmp_path` and `connectors_dir` is pointed at it, so nothing here depends
on which connectors the repo happens to ship today.
"""

from pathlib import Path

import pytest

from chemclaw.agent.chemclaw_agent import connector_specs
from chemclaw.connectors.manifest import HttpEndpoint, StdioEndpoint
from chemclaw.connectors.registry import (
    ConnectorError,
    connector_tool_names,
    declared_note_types,
    declared_relations,
    discovered,
    enabled,
    health_url,
    job_tools,
    server_tools_module,
    skills_dirs,
)
from chemclaw.kg.note import KNOWN_NOTE_TYPES, known_note_types
from chemclaw.kg.relations import KNOWN_RELATIONS, known_relations

_JOB_BLOCK = """
jobs:
  - name: run_thing
    workflow: ThingWorkflow
    summary: Run the thing.
    params:
      - {name: subject, type: string, description: What to run it on.}
"""


def _bundle(root: Path, name: str, body: str) -> Path:
    """Write one bundle directory with `connector.yaml` and return it."""
    bundle = root / name
    bundle.mkdir(parents=True)
    (bundle / "connector.yaml").write_text(body, encoding="utf-8")
    return bundle


def _http_manifest(name: str, port: int = 9001, tools: str = "search") -> str:
    """A minimal valid HTTP-endpoint manifest body."""
    return (
        f"name: {name}\n"
        f"description: the {name} capability\n"
        "endpoint:\n"
        "  transport: http\n"
        f"  url: http://127.0.0.1:{port}/mcp\n"
        f"  health_url: http://127.0.0.1:{port}/healthz\n"
        "  tools:\n"
        f"    - {tools}\n"
        "  read_only:\n"
        f"    - {tools}\n"
    )


def _use(monkeypatch: pytest.MonkeyPatch, root: Path, *, enabled_list: str = "") -> None:
    """Point the registry at `root` as its only connectors dir, with the given enable-list.

    Every test here calls this exactly once, before its first `discovered()`/`enabled()` call, so
    no local `cache_clear()` is needed: `tests/conftest.py`'s autouse fixture guarantees the cache
    is already empty when this test started.
    """
    monkeypatch.setattr("chemclaw.core.config.settings.connectors_dir", str(root))
    monkeypatch.setattr("chemclaw.core.config.settings.connectors_enabled", enabled_list)


def test_discovery_finds_bundles_by_folder_and_ignores_everything_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bundle is a folder with a manifest; a folder without one is not a half-broken connector."""
    _bundle(tmp_path, "alpha", _http_manifest("alpha"))
    (tmp_path / "notes").mkdir()  # a directory with no connector.yaml
    (tmp_path / "README.md").write_text("not a bundle", encoding="utf-8")
    _use(monkeypatch, tmp_path)
    assert list(discovered()) == ["alpha"]


def test_discovery_order_is_sorted_not_filesystem_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tool order is part of the prompt, so it must not vary by machine (reproducibility)."""
    for name in ("zulu", "alpha", "mike"):
        _bundle(tmp_path, name, _http_manifest(name))
    _use(monkeypatch, tmp_path)
    assert [manifest.name for manifest in enabled()] == ["alpha", "mike", "zulu"]


def test_an_empty_enable_list_means_every_discovered_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `skills_enabled` default: a fresh checkout runs the full shipped surface."""
    _bundle(tmp_path, "alpha", _http_manifest("alpha"))
    _bundle(tmp_path, "beta", _http_manifest("beta"))
    _use(monkeypatch, tmp_path)
    assert {manifest.name for manifest in enabled()} == {"alpha", "beta"}


def test_the_enable_list_narrows_and_orders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-empty list is both the subset *and* the order, because order is configuration."""
    for name in ("alpha", "beta", "gamma"):
        _bundle(tmp_path, name, _http_manifest(name))
    _use(monkeypatch, tmp_path, enabled_list="gamma:alpha")
    assert [manifest.name for manifest in enabled()] == ["gamma", "alpha"]


def test_an_unknown_enabled_connector_fails_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Advertising nothing silently is the failure this refuses: it looks like a broken tool."""
    _bundle(tmp_path, "alpha", _http_manifest("alpha"))
    _use(monkeypatch, tmp_path, enabled_list="alpha:ghost")
    with pytest.raises(ConnectorError, match="ghost"):
        enabled()


def test_a_manifest_name_must_match_its_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Otherwise it is enabled under one name and looked up under another — the `SKILL.md` rule."""
    _bundle(tmp_path, "alpha", _http_manifest("beta"))
    _use(monkeypatch, tmp_path)
    with pytest.raises(ConnectorError, match="lives in directory"):
        discovered()


def test_malformed_yaml_is_a_named_configuration_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A parse failure names the file, so it is fixable without reading a traceback."""
    _bundle(tmp_path, "alpha", "name: [unclosed\n")
    _use(monkeypatch, tmp_path)
    with pytest.raises(ConnectorError, match="alpha/connector.yaml"):
        discovered()


def test_each_transport_builds_its_matching_maf_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dispatch, and the allow-list carried through on both: the boundary is transport-free."""
    _bundle(tmp_path, "remote", _http_manifest("remote", tools="search"))
    _bundle(
        tmp_path,
        "local",
        "name: local\ndescription: a local capability\n"
        "endpoint:\n  transport: stdio\n  command: python\n  args: ['-m', 'x']\n"
        "  tools:\n    - compute\n  read_only:\n    - compute\n",
    )
    _use(monkeypatch, tmp_path)
    # Both transports as specs, which is the one shape now. It used to be two classes — a
    # `DegradingHttpConnector` and a `DegradingStdioConnector`, distinguished by `isinstance` —
    # because MAF took a live tool object per transport. `open_connector_specs` opens a session
    # from a `Connection` mapping, so what a registry builds is a description either way and the
    # transport shows up in the connection rather than in the type.
    built = {spec.name: spec for spec in connector_specs()}
    assert built["remote"].connection["transport"] == "streamable_http"
    assert built["local"].connection["transport"] == "stdio"
    assert list(built["remote"].allowed_tools or []) == ["search"]
    assert list(built["local"].allowed_tools or []) == ["compute"]


def test_connector_urls_override_the_manifest_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manifest ships a dev default; a cluster address belongs to the deployment."""
    _bundle(tmp_path, "alpha", _http_manifest("alpha"))
    _use(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "chemclaw.core.config.settings.connector_urls", {"alpha": "http://alpha.svc:8080/mcp"}
    )
    (spec,) = connector_specs()
    assert spec.connection.get("url") == "http://alpha.svc:8080/mcp"


def test_the_health_probe_follows_the_address_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe must go where the tools go, or readiness reports on the wrong host (D-131).

    The shipped chart always sets `connector_urls`, so before this the front door probed the
    manifest's loopback dev default in every cluster: every connector read `unreachable` however
    healthy it was, and `connectors_required: true` would have failed startup outright.
    """
    _bundle(tmp_path, "alpha", _http_manifest("alpha"))
    _use(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "chemclaw.core.config.settings.connector_urls",
        {"alpha": "http://alpha-connector.svc:8814/mcp"},
    )
    (manifest,) = enabled()
    assert health_url(manifest) == "http://alpha-connector.svc:8814/healthz"


def test_the_health_probe_follows_an_override_that_moves_the_path_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`chemclaw.cli.connectors_dev` mounts every bundle under one port by name, so the path moves.

    Swapping only the origin would give `/healthz`, which that composite serves as a 404 — the
    reason the dev topology could not tell a killed connector from a mis-probed one.
    """
    _bundle(tmp_path, "alpha", _http_manifest("alpha"))
    _use(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "chemclaw.core.config.settings.connector_urls", {"alpha": "http://127.0.0.1:8810/alpha/mcp"}
    )
    (manifest,) = enabled()
    assert health_url(manifest) == "http://127.0.0.1:8810/alpha/healthz"


def test_a_connector_declaring_no_health_route_stays_unprobed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A third-party MCP server may expose nothing; guessing a path would be a false alarm."""
    _bundle(
        tmp_path,
        "alpha",
        "name: alpha\ndescription: the alpha capability\n"
        "endpoint:\n  transport: http\n  url: http://127.0.0.1:9001/mcp\n"
        "  tools:\n    - search\n  read_only:\n    - search\n",
    )
    _use(monkeypatch, tmp_path)
    (manifest,) = enabled()
    assert health_url(manifest) is None


def test_a_jobs_only_connector_contributes_a_tool_and_no_mcp_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Durable capability needs no endpoint: the launcher is the whole agent-facing surface."""
    _bundle(tmp_path, "thing", f"name: thing\ndescription: durable only\n{_JOB_BLOCK}")
    _use(monkeypatch, tmp_path)
    assert connector_specs() == []
    (tool,) = job_tools()
    assert tool.__name__ == "run_thing"


def test_two_connectors_cannot_claim_one_job_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The job name is the authz key, so a collision applies one gate to another's work."""
    _bundle(tmp_path, "alpha", f"name: alpha\ndescription: one\n{_JOB_BLOCK}")
    _bundle(tmp_path, "beta", f"name: beta\ndescription: two\n{_JOB_BLOCK}")
    _use(monkeypatch, tmp_path)
    with pytest.raises(ConnectorError, match="already provides"):
        job_tools()


def test_a_job_cannot_take_the_name_of_another_connectors_endpoint_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The collision that was live: one name, an MCP tool on one bundle and a job on another.

    `props` served `compare_solvents` (a table lookup) while `calc` declared a durable job of the
    same name (a semiempirical calculation per species per solvent), and the deployment that brings
    them together is the documented one. It raised nothing: `connector_tool_names()` is a set union,
    so 30 declared names came back as 29 and the loser simply was not on the agent's surface.
    """
    _bundle(tmp_path, "alpha", _http_manifest("alpha", tools="run_thing"))
    _bundle(tmp_path, "beta", f"name: beta\ndescription: two\n{_JOB_BLOCK}")
    _use(monkeypatch, tmp_path)
    with pytest.raises(ConnectorError, match="already provides as a tool"):
        job_tools()


def test_one_connector_cannot_declare_a_job_and_a_tool_with_one_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Within a bundle too: the old dict keyed by job name would have absorbed this silently."""
    _bundle(tmp_path, "alpha", _http_manifest("alpha", tools="run_thing") + _JOB_BLOCK)
    _use(monkeypatch, tmp_path)
    with pytest.raises(ConnectorError, match="already provides as a tool"):
        job_tools()


def test_connector_tool_names_spans_endpoints_and_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What the skill and prose validators check against: both halves, since both are callable."""
    _bundle(
        tmp_path,
        "alpha",
        _http_manifest("alpha", tools="search") + _JOB_BLOCK,
    )
    _use(monkeypatch, tmp_path)
    assert connector_tool_names() == ["run_thing", "search"]


def test_only_declared_and_present_skill_dirs_are_advertised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bundled skills dir joins discovery; a declared-but-absent one is a packaging bug.

    Returning a non-existent path would fail the *agent* (the skills source raises on a missing dir)
    for a problem that belongs to `make connector-validate` — so a broken bundle degrades the skill
    surface, it does not break every turn.
    """
    with_skills = _bundle(tmp_path, "alpha", _http_manifest("alpha") + "skills:\n  - judgment\n")
    (with_skills / "skills" / "judgment").mkdir(parents=True)
    _bundle(tmp_path, "beta", _http_manifest("beta") + "skills:\n  - missing\n")
    _bundle(tmp_path, "gamma", _http_manifest("gamma"))
    _use(monkeypatch, tmp_path)
    assert skills_dirs() == [str(with_skills / "skills")]


def test_the_first_connectors_dir_wins_a_name_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`PATH` precedence: an operator's private bundle dir can override a shipped bundle."""
    private = tmp_path / "private"
    shipped = tmp_path / "shipped"
    _bundle(private, "alpha", _http_manifest("alpha", port=7777))
    _bundle(shipped, "alpha", _http_manifest("alpha", port=8888))
    monkeypatch.setattr("chemclaw.core.config.settings.connectors_dir", f"{private}:{shipped}")
    monkeypatch.setattr("chemclaw.core.config.settings.connectors_enabled", "")
    (manifest,) = enabled()
    assert isinstance(manifest.endpoint, HttpEndpoint | StdioEndpoint)
    assert "7777" in str(manifest.endpoint)


def test_a_bundle_contributes_note_types_without_a_core_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bundle's `note_types:` join the graph vocabulary, and leave with the bundle.

    **The gap this closes.** `job-result` and `bo-candidate` are minted by the `qm` and `bo`
    bundles, and both used to be hand-written lines in `chemclaw.kg.note.KNOWN_NOTE_TYPES` — so
    contributing a note type was the one connector contribution that required editing core, inside
    the seam whose whole claim is that a capability is a folder (D-118). Everything else a bundle
    gives (tools, jobs, skills, profiles, its queue, its pods) is declaration-only.

    Scoped to the *enabled* set, not the discovered one: a bundle a deployment does not run
    contributes no vocabulary either, so a note of its type correctly fails `kg-validate` there.
    """
    _bundle(tmp_path, "alpha", _http_manifest("alpha") + "note_types:\n  - assay-result\n")
    _bundle(tmp_path, "beta", _http_manifest("beta") + "note_types:\n  - shelved\n")
    _use(monkeypatch, tmp_path, enabled_list="alpha")

    assert declared_note_types() == frozenset({"assay-result"})
    assert "assay-result" in known_note_types()
    assert "shelved" not in known_note_types(), "a disabled bundle contributes no vocabulary"
    assert "assay-result" not in KNOWN_NOTE_TYPES, "core's own set is unchanged"


def test_a_bundle_contributes_relations_the_same_way(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The edge-side twin, so `kg-validate` accepts an edge a bundle's notes actually draw."""
    _bundle(tmp_path, "alpha", _http_manifest("alpha") + "relations:\n  - assayed-by\n")
    _use(monkeypatch, tmp_path)

    assert declared_relations() == frozenset({"assayed-by"})
    assert "assayed-by" in known_relations()
    assert "assayed-by" not in KNOWN_RELATIONS


def test_a_malformed_vocabulary_name_is_refused_at_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A name that is not a lowercase hyphenated token cannot enter the vocabulary.

    The door opened for extending a closed set must not let through exactly what closing it
    prevented: a note type becomes a path segment (`knowledge/<type>/<id>.md`), so a name with a
    slash or a capital would produce a note that validates and is then unfindable by every filter
    keyed on its type.
    """
    _bundle(tmp_path, "alpha", _http_manifest("alpha") + "note_types:\n  - Assay Result\n")
    _use(monkeypatch, tmp_path)
    with pytest.raises(ConnectorError, match="lowercase hyphenated"):
        discovered()


def test_a_bundle_with_no_server_package_has_no_server_module() -> None:
    """`server_tools_module` returns `None` for a bundle this repository declares and does not run.

    **This is the case that used to raise, and CI is the only place it showed.** The function
    distinguishes "no server module" from "the server module is broken underneath" by comparing
    `exc.name` against the module it asked for — which was complete while every endpoint-bearing
    bundle shipped a `server/` directory. `chem`'s capability moved to `Chemclaw3-mcp` and the
    directory went with it, so the *parent package* is what is missing and `exc.name` is the
    package. The function raised where its own docstring says it returns `None`, and every caller —
    `make connector-validate`, `make template-validate`, and the transport tests' parametrization —
    died at import.

    It passed locally throughout, off the same commit, and the reason is worth a sentence because it
    will recur: a deleted `server/` leaves its `__pycache__` behind, so the directory survives as a
    PEP 420 namespace package, the import gets one level further, and the error names the module
    after all. There was no test at all before this one, which is what let a documented three-way
    contract be checked by nothing.
    """
    assert server_tools_module("chem") is None


def test_a_jobs_only_bundle_has_no_server_module() -> None:
    """The other `None`: `results` declares no endpoint and ships no server, and never has."""
    assert server_tools_module("results") is None


def test_a_bundle_that_serves_tools_returns_its_module() -> None:
    """The positive case, so the two above cannot pass by the function returning `None` always."""
    module = server_tools_module("calc")
    assert module is not None and hasattr(module, "server")


def test_a_broken_dependency_underneath_a_server_still_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The half that must *not* be swallowed, and the reason the predicate is a set of two names.

    A missing or renamed dependency underneath a real server means the bundle is broken. Swallowing
    it leaves a validator checking less and still reporting success — measured once already, where
    `validate_templates` resolved 46 signatures instead of 50 and printed "template validation
    passed" for a bundle that could not be imported at all.
    """
    import importlib

    real = importlib.import_module

    def _missing_dep(name: str, *args: object, **kwargs: object) -> object:
        if name.endswith(".server.tools"):
            raise ModuleNotFoundError("No module named 'absent_dep'", name="absent_dep")
        return real(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(importlib, "import_module", _missing_dep)
    with pytest.raises(ModuleNotFoundError, match="absent_dep"):
        server_tools_module("calc")
