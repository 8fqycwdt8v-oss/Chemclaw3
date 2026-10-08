"""Write the OpenAPI document the front door publishes to `schema/api/openapi.json`.

    python -m chemclaw.cli.openapi            # regenerate the committed file (make openapi)

Offline: the app is built in-process and never started. The logic is `chemclaw.api.contract`.
"""

from chemclaw.api.contract import CONTRACT_PATH, build_document, render_document


def main() -> None:
    """Regenerate the committed contract file and say where it went."""
    CONTRACT_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONTRACT_PATH.write_text(render_document(build_document()), encoding="utf-8")
    print(f"wrote {CONTRACT_PATH}")


if __name__ == "__main__":
    main()
