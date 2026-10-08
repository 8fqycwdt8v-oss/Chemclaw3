"""The published API contract: its version, and the document rendered from the running app.

`Chemclaw3_ui` generates its types from `schema/api/openapi.json`, so that file is a published
contract with one owner. `API_CONTRACT_VERSION` is its only version and becomes `info.version`;
the bump rules are in `schema/api/README.md`. The document is built by `create_app()` in-process
(no network, no database) and rendered with sorted keys, so two runs produce the same bytes.

Invariant: `render_document` is the only serialiser of the committed file; the staleness test and
`make openapi` both call it.
"""

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, TypeAdapter

from chemclaw.protocols.render import ProtocolReadout

#: Semver of the document. Major: a field, event or route removed, renamed or retyped. Minor:
#: anything additive. Patch: wording only.
API_CONTRACT_VERSION = "1.0.0"

#: Where the contract is committed, and the `make` target that regenerates it.
CONTRACT_PATH = Path(__file__).resolve().parents[3] / "schema" / "api" / "openapi.json"
REGENERATE_COMMAND = "make openapi"

#: Payloads the UI renders that no route returns by name: they ride inside a tool result
#: (`GET /sessions/{id}/tool-results/{ref}` serves the text). Published so the UI types them from
#: the same document.
PAYLOAD_MODELS: tuple[type[BaseModel], ...] = (ProtocolReadout,)


def payload_schemas() -> dict[str, Any]:
    """Every OpenAPI component the tool-result payload models need, keyed by component name."""
    components: dict[str, Any] = {}
    for model in PAYLOAD_MODELS:
        schema = TypeAdapter(model).json_schema(ref_template="#/components/schemas/{model}")
        components.update(schema.pop("$defs", {}))
        components[model.__name__] = schema
    return components


def build_document() -> dict[str, Any]:
    """The OpenAPI document `create_app()` serves, built without starting anything.

    The startup refusals are satisfied (loopback bind, loopback-gateway acknowledgement, as `make
    chat` does) rather than patched out, and the previous settings are restored.
    """
    from chemclaw.api.app import create_app
    from chemclaw.core.config import settings

    previous = (settings.service_host, settings.llm_allow_loopback_gateway)
    settings.service_host, settings.llm_allow_loopback_gateway = "127.0.0.1", True
    try:
        return dict(create_app().openapi())
    finally:
        settings.service_host, settings.llm_allow_loopback_gateway = previous


def render_document(document: dict[str, Any]) -> str:
    """The document as committed: sorted keys, two-space indent, one trailing newline."""
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
