"""No first-party module reaches an external host (D-089: this system takes no external sources).

Infrastructure the operator runs (LLM endpoint, Temporal, Postgres, Entra, the knowledge git
remote) is configured per deployment and allowed. What is banned is a hardcoded third-party data
source baked into first-party code, so the check is on host literals in source, not on the
ability to make a request.
"""

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]

# First-party code only: tests may name hosts in docstrings or mocked transports, and the rule is
# about what ships. Every first-party module lives under `src/`.
_PACKAGES = ("src",)

# Hosts the system is deployed with rather than reaching out to. Everything else the stack talks to
# is a required config value with no host default in source; Entra's login host is the exception
# because the tenant is substituted into Microsoft's host.
_INFRASTRUCTURE_HOSTS = {
    "login.microsoftonline.com",
}

# Userinfo is skipped, not captured: the host of `https://svc:token@llm.internal/v1` is what
# follows the `@`.
_URL = re.compile(r"https?://(?:[^/@\s]*@)?([A-Za-z0-9.-]+)")

# Our own in-cluster Services (`chemclaw-connector-<name>`) and loopback. A prefix rule so adding a
# connector does not require editing this test.
_INTERNAL_PREFIXES = ("chemclaw-", "127.0.0.1", "localhost")


def _host_literals() -> dict[str, set[str]]:
    """Every http(s) host literal in first-party source, keyed by repo-relative file path."""
    found: dict[str, set[str]] = {}
    for package in _PACKAGES:
        for path in (_ROOT / package).rglob("*.py"):
            hosts = {
                host
                for host in _URL.findall(path.read_text(encoding="utf-8"))
                if host not in _INFRASTRUCTURE_HOSTS and not host.startswith(_INTERNAL_PREFIXES)
            }
            if hosts:
                found[str(path.relative_to(_ROOT))] = hosts
    return found


def test_no_module_hardcodes_a_third_party_data_source() -> None:
    """No shipped module names an external data host.

    Fails with the file and host. Adopting new infrastructure means adding it to
    `_INFRASTRUCTURE_HOSTS` in the same commit, which is the review moment this test forces.
    """
    offenders = _host_literals()
    assert not offenders, "first-party code names external hosts: " + "; ".join(
        f"{path} → {sorted(hosts)}" for path, hosts in sorted(offenders.items())
    )


def test_the_source_registry_offers_no_external_source() -> None:
    """The data sources this repository ships are checked by name.

    A host-literal scan cannot catch a source whose address comes from config, and nothing becomes a
    retrievable source without a `datasource.yaml`. A deployment mounting extra source folders is
    its own decision to audit; what must not drift unnoticed is a new external corpus arriving here.
    """
    from chemclaw.ingest.sources.registry import discovered

    assert "literature" not in discovered()
    assert set(discovered()) == {
        "graph",
        "vector",
        "lexical",
        "eln-json",
        "eln-ord",
        # A programme's committed work, read from a JSON extract on disk; it reaches no host.
        "commitments-json",
        # Reads a corpus baked into the image at build time (STO-14). Named here rather than
        # exempted: it is the one sanctioned escalation of D-089's scope, and it earns its place
        # by being *local* — the two tests below hold it to that.
        "vendored",
        # The corporate ELN in a lakehouse: a remote host, sanctioned because it is the deployment's
        # own system rather than a third-party corpus. Its address and credentials come only from
        # config and it ships disabled. A second SQL warehouse is a different `connection.driver:`,
        # not a new row.
        "eln-databricks",
        # The site's own licensed patent corpus in its own lakehouse, reached through the same
        # connection as `eln-databricks`, so it adds no destination. Retrieve-only: a patent
        # reaction is cited as precedent and never becomes a note. See
        # `docs/decisions/D-2026-08-25-a-lakehouse-arrives-on-two-seams-not-one.md`.
        "pistachio",
        # A mounted read-only SMB/CIFS share: the code sees a POSIX path, with no client, credential
        # or network peer. Ships disabled.
        "sharedrive",
    }


def test_the_vendored_source_cannot_make_a_request() -> None:
    """The vendored dataset module may not import an HTTP client.

    A build-time dataset has no runtime dependency on anyone's service; this makes that structural
    so a later edit cannot acquire one by accident.
    """
    source = (_ROOT / "src" / "chemclaw" / "ingest" / "sources" / "vendored_dataset.py").read_text(
        encoding="utf-8"
    )
    for client in ("httpx", "requests", "urllib", "aiohttp", "socket"):
        assert f"import {client}" not in source, (
            f"ingest/sources/vendored_dataset.py imports {client}: a vendored dataset is "
            "installed at build time and read from local disk, and the moment it can make a "
            "request it is an "
            "external data source (D-089)"
        )


def test_the_vendored_dataset_declares_its_provenance() -> None:
    """A shipped corpus carries a licence, a version and a checksum, or it does not load.

    The review that admits it is only meaningful if the reviewed thing is identifiable later, so the
    schema requires these fields.
    """
    import json

    manifest = json.loads(
        (_ROOT / "data" / "vendored" / "dataset.json").read_text(encoding="utf-8")
    )
    for field in ("name", "version", "licence", "retrieved_from", "sha256"):
        assert manifest.get(field), f"vendored dataset manifest is missing {field}"
