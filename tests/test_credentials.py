"""Every credential on `Settings` is a `SecretStr`, and every consumer still sends the real value.

Two assertions that look like one and are not
(`D-2026-08-26-a-credential-is-a-type-not-a-convention`):

- **The type.** A `SecretStr` reprs as `**********`, so the value cannot reach a dump, a pydantic
  error message or a debugger through a route `core/logging.py`'s exact-match redaction has not been
  taught about. That filter stays and is still the control; this is the same guarantee where the
  filter is not looking.
- **The transmission.** An f-string does *not* unwrap a `SecretStr`, so a credential formatted
  into a header, a signature or a client option compiles, runs, and sends `**********`. The call
  then fails as a 401 rather than leaking — the right direction, and still a failure. "The
  credential is present" and "the credential is correct" are different assertions, and only the
  second one catches it. Every consumer below asserts the second.

The two halves are tested together on purpose: a change that satisfies either alone is a regression.
"""

import re

import pytest
from pydantic import SecretStr

from chemclaw.core.config import Settings, settings
from chemclaw.core.logging import _SECRET_SETTINGS, redact_secrets

# The three DSNs, explicitly out of the type change: 34 lines across 27 modules read one, all
# feeding psycopg conninfo, which needs the plain string straight back. They are still redacted —
# they are in `_SECRET_SETTINGS` — which is why this list is an exception to the type rule rather
# than to the inventory.
_DSNS = frozenset({"postgres_dsn", "postgres_migration_dsn", "session_store_dsn"})


def test_every_redacted_setting_that_is_not_a_dsn_is_a_secret_str() -> None:
    """Driven off the redaction inventory, so the two lists cannot drift apart.

    A new credential added as a plain `str` fails here; a `SecretStr` added without a
    `_SECRET_SETTINGS` row fails in the test below. Together that is "declare it once and both
    protections follow", which is the property worth having — the previous arrangement lost
    `llm_fallback_api_key` from the inventory entirely, and it was the one credential in `Settings`
    that nothing redacted at all.
    """
    plain = sorted(
        name
        for name in _SECRET_SETTINGS
        if name not in _DSNS
        and not isinstance(type(settings).model_fields[name].default, SecretStr)
    )
    assert plain == [], f"{plain} are credentials typed as plain `str`"


def test_every_secret_str_on_the_settings_object_is_also_redacted() -> None:
    """The other direction: a typed credential the log filter has never heard of.

    The type hides a value from a `repr`; the filter catches it in a log line that quoted it some
    other way. Neither subsumes the other, so a field with one and not the other is half protected
    and reads as fully protected.
    """
    typed = {
        name
        for name, field in type(settings).model_fields.items()
        if isinstance(field.default, SecretStr)
    }
    assert typed - set(_SECRET_SETTINGS) == set(), "a SecretStr no log line would redact"


# What a credential-shaped field name ends in. **Anchored, and read together with the type
# below** — which is what separates this from the name-pattern heuristic
# `core/logging.py`'s own comment rejects and `tests/test_logging.py` restates. That heuristic
# was unanchored and untyped, so it swept in `calc_server_token_env` (a variable *name*),
# `budget_max_tokens_per_user` (an integer) and `temporal_tls_key` (a path to a PEM). None of the
# three survives this one: `_env` is not a terminal credential word, an `int` is not a `str`, and
# `api_key` does not match `tls_key`.
#
# The residual is a credential whose name ends in none of these words. That is a real gap and it is
# the *reason* the inventory stays a hand-written list — this guard cannot replace the judgement,
# only make the common shape impossible to forget.
_CREDENTIAL_NAME = re.compile(r"(api_key|token|secret|password|dsn|credential)$")


def _credential_shaped(model: type[Settings]) -> set[str]:
    """Every field of `model` whose name and type both say "this holds a credential"."""
    return {
        name
        for name, field in model.model_fields.items()
        if _CREDENTIAL_NAME.search(name) and field.annotation in (str, SecretStr)
    }


def test_every_credential_shaped_setting_is_in_the_redaction_inventory() -> None:
    """The direction neither existing test covers: a credential added as a plain `str`.

    The two tests above are a closed loop over `_SECRET_SETTINGS` and the `SecretStr` fields — a
    field in *neither* is invisible to both, which is exactly how `vector_store_api_key` and
    `live_probe_token` sat outside every redaction mechanism while the suite stayed green. Each was
    found by a separate audit reading the file; nothing failed.

    Asserted in both directions, so this is also the row-in-review the inventory's comment asks for:
    adding a credential-shaped setting fails here until it is listed, and listing a field that no
    longer looks like a credential fails here too.
    """
    shaped = _credential_shaped(Settings)
    assert shaped == set(_SECRET_SETTINGS), (
        f"credential-shaped but not redacted: {sorted(shaped - set(_SECRET_SETTINGS))}; "
        f"redacted but no longer credential-shaped: {sorted(set(_SECRET_SETTINGS) - shaped)}"
    )


def test_the_guard_above_fires_for_a_credential_nobody_has_added_yet() -> None:
    """A guard that is green because it can never fail is not a guard.

    The assertion above passes today by construction — every name it finds is already listed — so
    on its own it says nothing about the *next* credential. This adds one, the way a future
    settings section would, and asserts the shape-check sees it.
    """

    class _Later(Settings):
        probe_api_key: str = ""
        probe_service_token: str = ""

    added = _credential_shaped(_Later) - _credential_shaped(Settings)
    assert added == {"probe_api_key", "probe_service_token"}


def test_every_bearer_named_by_a_setting_is_redacted_by_its_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The third shape of credential setting, invisible to both directions above by construction.

    `calc_server_token_env`, `rxnlabel_server_token_env` and `mcp_face_token_env` hold a variable
    *name*, not a value — which is why `_CREDENTIAL_NAME` deliberately does not match `_env`, and
    why a `SecretStr` would be the wrong type for them. The consequence was that the bearer each
    one points at was outside every mechanism: not in `_SECRET_SETTINGS` (whose members are
    values), not a connector manifest's `token_env`, and never passed to `register_secret_env`.
    Measured, all three survived `redact_secrets` verbatim — including the one guarding the
    read-only MCP face and the one for the calculation backend, "the hottest and most privileged
    connection in the system".

    Driven off `Settings.model_fields` rather than the three names, so a fourth such setting is
    covered on the day it is declared. The assertion is on the *value*, since a bearer in a log
    line or a rendered traceback rarely arrives beside its variable name.
    """
    bearers = {}
    for name in sorted(Settings.model_fields):
        if not name.endswith("_token_env"):
            continue
        variable = getattr(settings, name)
        bearers[name] = f"{variable}-s3cretVALUE0123456789"
        monkeypatch.setenv(variable, bearers[name])

    assert bearers, "no `*_token_env` setting found; this test has lost its subject"
    leaked = sorted(
        name for name, value in bearers.items() if value in redact_secrets(f"upstream sent {value}")
    )
    assert leaked == [], f"{leaked} name a bearer no log line would redact"


def test_a_secret_str_hides_its_value_from_the_shapes_that_leak() -> None:
    """The premise of the whole change, asserted rather than believed.

    Both are pydantic behaviour rather than ours — which is exactly why they are pinned here: if a
    future pydantic renders the value in either, this change stops buying anything and the ADR's
    argument is void.
    """
    holder = settings.model_copy(update={"llm_api_key": SecretStr("sk-real-value")})
    assert "sk-real-value" not in repr(holder.llm_api_key)
    assert "sk-real-value" not in str(holder.model_dump())
    assert holder.llm_api_key.get_secret_value() == "sk-real-value"


def test_the_webhook_signature_is_computed_over_the_real_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An HMAC over `**********` verifies against itself, so both sides agree and neither is signed.

    The worst shape of this failure: a signature check that passes for the caller who holds the
    same wrapper and rejects the caller who holds the actual secret — a control that keeps working
    while protecting nothing.
    """
    import hashlib
    import hmac

    from chemclaw.api.routes.proposals import _webhook_signature_ok

    monkeypatch.setattr(settings, "note_webhook_secret", SecretStr("s3cret"))
    body = b'{"note_id":"n-1"}'
    signature = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert _webhook_signature_ok(body, signature)
    assert not _webhook_signature_ok(body, "sha256=" + "0" * 64)


def test_the_envelope_nonce_is_derived_from_the_real_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deployment-stable nonce that is stable across deployments *for the wrong reason*.

    `framing_envelope_secret` is not a credential to anything — it is the HMAC key the envelope tag
    is derived from, and the agent instructions say only an envelope carrying exactly that tag marks
    retrieved content as data. Derived from `**********` instead, every deployment that set *any*
    secret would share one nonce, which is precisely the property the secret exists to prevent.
    """
    import hmac
    from hashlib import sha256

    from chemclaw.agent.framing import _envelope_nonce

    monkeypatch.setattr(settings, "framing_envelope_secret", SecretStr("envelope-key"))
    expected = hmac.new(b"envelope-key", b"chemclaw-retrieved-note-envelope", sha256).hexdigest()[
        :16
    ]
    assert _envelope_nonce() == expected


def test_the_temporal_client_passes_the_key_it_was_given(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one consumer that hands the value to a third-party client rather than formatting it."""
    from chemclaw.core.temporal_client import connect_options

    monkeypatch.setattr(settings, "temporal_api_key", SecretStr("temporal-key"))
    assert connect_options()["api_key"] == "temporal-key"
    monkeypatch.setattr(settings, "temporal_api_key", SecretStr(""))
    assert "api_key" not in connect_options()
