"""No first-party module reaches an external host (the "no external sources" decision, D-089).

The PubChem literature retriever was built, reviewed, and then rejected on scope: **this system
takes no external sources**. Deleting the module records that as of today; it does not stop the
next connector. The reason a guard is warranted rather than a `DEFERRED.md` line is that the
original decision *was* already written down — TOOL-6 sat in `DEFERRED.md` as "blocked on choosing a
source", which reads as an invitation, and duly got built. Prose stated the constraint; nothing
enforced it.

**What "external" means here, precisely.** Not "no URLs" — the codebase legitimately holds the
addresses of things it is deployed alongside: the LLM endpoint, Temporal, Postgres, the Entra
token endpoint, the Nextflow/Tower API, the git remote for the knowledge repo. Those are
*infrastructure the operator runs or contracts for*, configured per deployment and pointed at
internal hosts. What is banned is a **hardcoded third-party data source** — a literature API, a
structure lookup, a vendor catalogue — where the address of somebody else's service is baked into
first-party code.

So the check is on **defaults in source, not on the ability to make a request**: any `https://`
host literal that is not on the infrastructure allowlist is a finding. A deployment that points
`llm_base_url` at a public endpoint is the operator's call and outside this test's reach; a module
that ships pointing at `pubchem.ncbi.nlm.nih.gov` is not.
"""

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]

# First-party packages. `tests/` is excluded deliberately: a test may legitimately name a host in a
# docstring or a mocked-transport URL, and the constraint is about what the *shipped* code reaches.
_PACKAGES = (
    "agents",
    "bo",
    "calc",
    "chemclaw",
    "connectors",
    "eln",
    "evals",
    "infra",
    "kg",
    "mcp_servers",
    "memory",
    "report",
    "scripts",
    "service",
    "sources",
    "workers",
    "workflows",
)

# Hosts the system is *deployed with* rather than reaching out to: infrastructure an operator runs
# or contracts for, appearing only as a per-deployment configurable default.
#
# Exactly one, which is the useful fact this list records. Everything else the stack talks to — the
# LLM endpoint, Temporal, Postgres, Tower, the git remote — carries no host default in source at
# all; it is a required config value, so a deployment cannot accidentally inherit somebody's
# address. Entra's login host is the exception because it genuinely is Microsoft's: the tenant is
# substituted into it, and that identity provider is the architecture's choice (F4), not a default
# that could point elsewhere.
_INFRASTRUCTURE_HOSTS = {
    "login.microsoftonline.com",
}

_URL = re.compile(r"https?://([A-Za-z0-9.-]+)")

# Our own in-cluster Services and loopback. A connector is a Chemclaw component we deploy, reached
# at
# `chemclaw-connector-<name>` (the Helm Service) or at a loopback port in dev, so its address is the
# *most* internal kind of host there is — the opposite of the third-party data source this guard
# exists to catch. A prefix rule rather than one exemption per bundle: adding a connector must not
# require editing this test, or the guard becomes friction that gets weakened instead of respected.
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
    """No shipped module names an external data host — the enforceable form of the decision.

    Fails loudly with the file and host, so the finding is actionable rather than a puzzle. If a
    genuinely new piece of *infrastructure* is adopted, it is added to `_INFRASTRUCTURE_HOSTS` in
    the same commit that adopts it — which is exactly the review moment this test exists to force.
    """
    offenders = _host_literals()
    assert not offenders, "first-party code names external hosts: " + "; ".join(
        f"{path} → {sorted(hosts)}" for path, hosts in sorted(offenders.items())
    )


def test_the_source_registry_offers_no_external_source() -> None:
    """The data-source registry is the one place a source is attached, so it is checked by name.

    A host-literal scan cannot catch a source whose address arrives entirely from config. The
    registry is the chokepoint: nothing becomes a retrievable source without an entry here.
    """
    from sources.registry import DATA_SOURCES

    assert "literature" not in DATA_SOURCES
    assert set(DATA_SOURCES) == {
        "graph",
        "vector",
        "lexical",
        "eln-json",
        "eln-ord",
    }
