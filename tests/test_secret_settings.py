"""A credential setting is a `SecretStr`, and the ways that fails silently are guarded.

`SecretStr` closes a leak the log redactor structurally cannot: `core.logging` scrubs secret values
out of *log records*, and nothing was scrubbing `repr(settings)` reaching a response body, an
exception message, a `model_dump()` or a debugger. Measured before the conversion — `repr`, `str`,
`model_dump` and a JSON dump of the settings object each contained the API key verbatim.

**The conversion's own failure mode is the reason for most of this file.** A `SecretStr` renders as
`**********`, so a reader that was not converted does not crash and does not fail type-checking — it
sends the mask. Four sinks in this repo hide it from `mypy --strict` in three different ways:

- an **f-string** (`f"Bearer {settings.hpc_api_token}"`) — any object formats, so the type is fine
  and the credential becomes ten asterisks;
- an **`Any`-typed sink** (`options["api_key"] = ...` on a `dict[str, Any]`) — the object is stored
  whole and reaches the broker;
- an **`lru_cache`-wrapped callee** (`core.embeddings._openai_client`) — typeshed erases the wrapped
  signature to `Hashable`, so a `SecretStr` passed where `str` is annotated type-checks cleanly.

`mypy` found three readers and missed those last two entirely. So the guard here is a source scan
for the shape rather than a type check, plus a value-level assertion at each real sink.
"""

import ast
import json
from pathlib import Path

import pytest
from pydantic import SecretStr

from chemclaw.core.config import Settings, settings
from chemclaw.core.logging import _SECRET_SETTINGS

_SRC = Path(__file__).resolve().parents[1] / "src" / "chemclaw"

# The credential settings converted to `SecretStr`. Deliberately *not* derived from
# `_SECRET_SETTINGS`: that inventory also holds the three DSNs, which stay `str` until the shared
# Postgres connect helper lands (they are read directly in 26 modules today, which is the same
# duplication `BACKLOG.md` records as "the connect helper is hand-rolled 14 times" — converting
# first would mean writing 26 `.get_secret_value()` calls in order to delete 25 of them).
_SECRET_STR_FIELDS = frozenset(
    {
        "llm_api_key",
        "hpc_api_token",
        "hpc_artifact_store_token",
        "temporal_api_key",
        "note_webhook_secret",
        "audit_anchor_secret",
    }
)


def test_every_converted_credential_is_actually_a_secret_str() -> None:
    """The annotation, not the intent: a field reverted to `str` fails here, not silently."""
    for name in sorted(_SECRET_STR_FIELDS):
        annotation = Settings.model_fields[name].annotation
        assert annotation is SecretStr, f"{name} is {annotation}, not SecretStr"


def test_the_converted_fields_are_all_in_the_redaction_inventory() -> None:
    """Two layers over one credential, and neither is allowed to quietly stop covering it.

    `SecretStr` guards the non-logging paths (a repr, a dump, a response body); the redactor guards
    the logging path, including tracebacks, where a secret arrives as an already-rendered string
    that no type can mask. A credential in one and not the other is half-covered.
    """
    assert _SECRET_STR_FIELDS <= set(_SECRET_SETTINGS)


def test_a_settings_repr_no_longer_carries_a_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """The measured leak, in the four shapes it was measured in.

    Before the conversion every one of these contained the key verbatim. The redactor never saw
    them because none of them is a log record.
    """
    monkeypatch.setattr(settings, "llm_api_key", SecretStr("sk-live-LEAKCANARY-0123456789"))
    dumped = settings.model_dump()
    assert "LEAKCANARY" not in repr(settings)
    assert "LEAKCANARY" not in str(settings)
    assert "LEAKCANARY" not in json.dumps(dumped, default=str)
    assert "LEAKCANARY" not in str(dumped["llm_api_key"])
    # And the value is still reachable where it is meant to be, or this would pass on a broken app.
    assert settings.llm_api_key.get_secret_value() == "sk-live-LEAKCANARY-0123456789"


def _formatted_secret_settings() -> list[str]:
    """Every `f"...{settings.<secret>}..."` in `src/`, as `path:line` — the silent-mask trap.

    An AST walk rather than a grep so a formatted value split across lines is still found and a
    mention inside a docstring is not. It looks only for the *direct* interpolation of a secret
    field: `settings.x.get_secret_value()` is an `ast.Call` and correctly does not match.
    """
    found: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FormattedValue):
                continue
            value = node.value
            if (
                isinstance(value, ast.Attribute)
                and value.attr in _SECRET_STR_FIELDS
                and isinstance(value.value, ast.Name)
                and value.value.id == "settings"
            ):
                found.append(f"{path.relative_to(_SRC.parent.parent)}:{node.lineno}")
    return found


def test_no_credential_is_interpolated_into_an_f_string() -> None:
    """The failure `mypy` cannot see: an f-string renders a `SecretStr` as its mask.

    `f"Bearer {settings.hpc_api_token}"` type-checks perfectly and sends `Bearer **********`. The
    call then fails with a 401 somewhere else entirely, which is a much worse day than a type error
    — and on the artifact-store path it would have failed *silently*, since an unauthenticated fetch
    is a supported configuration there.
    """
    formatted = _formatted_secret_settings()
    assert not formatted, (
        "a credential setting is interpolated directly into an f-string, which renders it as "
        f"'**********' rather than the secret: {formatted}. Use `.get_secret_value()`."
    )


def test_the_launcher_header_carries_the_real_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Asserted on the header's value, because its *presence* was never the thing at risk."""
    from chemclaw.connectors.qm.hpc.nextflow import _auth_headers

    monkeypatch.setattr(settings, "hpc_api_token", SecretStr("launcher-token-123"))
    assert _auth_headers() == {"Authorization": "Bearer launcher-token-123"}


def test_an_absent_launcher_token_sends_no_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """`SecretStr("")` is falsy, so the "no credential" branch survives the conversion."""
    from chemclaw.connectors.qm.hpc.nextflow import _auth_headers

    monkeypatch.setattr(settings, "hpc_api_token", SecretStr(""))
    assert _auth_headers() == {}


def test_the_artifact_store_header_carries_its_own_real_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cross-origin path, where a masked token would have failed *silently*.

    An unauthenticated artifact fetch is a supported configuration (the launcher token must never
    cross origins), so a `Bearer **********` here would have been indistinguishable from a store
    that simply rejects us — the quietest of the four sinks.
    """
    from chemclaw.connectors.qm.hpc.nextflow import _artifact_headers

    monkeypatch.setattr(settings, "hpc_artifact_store_token", SecretStr("store-token-456"))
    assert _artifact_headers() == {"Authorization": "Bearer store-token-456"}


def test_the_temporal_api_key_reaches_the_client_as_a_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The `Any`-typed sink: a dict would have stored the `SecretStr` object itself."""
    from chemclaw.core.temporal_client import connect_options

    monkeypatch.setattr(settings, "temporal_api_key", SecretStr("cloud-key-789"))
    assert connect_options()["api_key"] == "cloud-key-789"


def test_the_webhook_signature_is_computed_over_the_real_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A MAC over the mask verifies nothing, and would have looked like a signing mismatch."""
    import hashlib
    import hmac

    from chemclaw.api.routes.proposals import _webhook_signature_ok

    secret = "webhook-secret-abc"
    body = b'{"merged": true}'
    monkeypatch.setattr(settings, "note_webhook_secret", SecretStr(secret))
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert _webhook_signature_ok(body, f"sha256={digest}")


def test_the_anchor_is_signed_with_the_real_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """The audit-chain anchor, whose whole value is that its key is not guessable."""
    import hashlib
    import hmac

    from chemclaw.agent.audit_anchor import Anchor, sign

    secret = "anchor-secret-def"
    monkeypatch.setattr(settings, "audit_anchor_secret", SecretStr(secret))
    anchor = Anchor(taken_at="2026-08-06T00:00:00Z", row_count=7, max_event_id=7, tip_hash="dead")
    expected = hmac.new(secret.encode(), anchor.payload().encode(), hashlib.sha256).hexdigest()
    assert sign(anchor) == expected
