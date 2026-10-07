"""Every credential on `Settings` is a `SecretStr`, and every consumer still sends the real value.

Two separate guarantees, tested together because a change satisfying either alone is a
regression:

- **The type.** A `SecretStr` reprs as `**********`, so the value cannot reach a dump, a
  pydantic error or a debugger where `core/logging.py`'s redaction is not looking.
- **The transmission.** An f-string does *not* unwrap a `SecretStr`, so a credential formatted
  into a header or signature would send `**********`. Every consumer below asserts the real
  value is sent.
"""

import re
from urllib.parse import urlsplit

import pytest
from pydantic import SecretStr

from chemclaw.core.config import Settings, settings
from chemclaw.core.logging import _SECRET_SETTINGS, redact_secrets

# The three DSNs stay plain strings because psycopg conninfo needs them; they are still redacted
# via `_SECRET_SETTINGS`, so this is an exception to the type rule, not to the inventory.
_DSNS = frozenset({"postgres_dsn", "postgres_migration_dsn", "session_store_dsn"})


def test_every_redacted_setting_that_is_not_a_dsn_is_a_secret_str() -> None:
    """Driven off the redaction inventory, so the two lists cannot drift apart.

    A credential added as a plain `str` fails here; a `SecretStr` without a `_SECRET_SETTINGS` row
    fails below. Declare it once and both protections follow.
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

    The type hides a value from `repr`; the filter catches it quoted some other way in a log line.
    Neither subsumes the other.
    """
    typed = {
        name
        for name, field in type(settings).model_fields.items()
        if isinstance(field.default, SecretStr)
    }
    assert typed - set(_SECRET_SETTINGS) == set(), "a SecretStr no log line would redact"


# What a credential-shaped field name ends in. Anchored, and read together with the `str` type
# below, so it does not sweep in `*_token_env` (a variable name), `*_max_tokens_*` (an int) or
# `temporal_tls_key` (a path).
#
# A credential whose name ends in none of these words is missed, which is why the inventory stays
# a hand-written list; this guard only makes the common shape impossible to forget.
_CREDENTIAL_NAME = re.compile(r"(api_key|token|secret|password|dsn|credential)$")


def _credential_shaped(model: type[Settings]) -> set[str]:
    """Every field of `model` whose name and type both say "this holds a credential"."""
    return {
        name
        for name, field in model.model_fields.items()
        if _CREDENTIAL_NAME.search(name) and field.annotation in (str, SecretStr)
    }


def test_every_credential_shaped_setting_is_in_the_redaction_inventory() -> None:
    """A credential added as a plain `str` and listed nowhere must be caught.

    The two tests above are a closed loop over `_SECRET_SETTINGS` and the `SecretStr` fields, so a
    field in neither is invisible to both. Both directions: a credential-shaped setting must be
    listed, and a listed field must still look like a credential.
    """
    shaped = _credential_shaped(Settings)
    assert shaped == set(_SECRET_SETTINGS), (
        f"credential-shaped but not redacted: {sorted(shaped - set(_SECRET_SETTINGS))}; "
        f"redacted but no longer credential-shaped: {sorted(set(_SECRET_SETTINGS) - shaped)}"
    )


def test_the_guard_above_fires_for_a_credential_nobody_has_added_yet() -> None:
    """A guard that is green because it can never fail is not a guard.

    The assertion above passes by construction today; this adds a credential the way a future
    settings section would and asserts the shape-check sees it.
    """

    class _Later(Settings):
        probe_api_key: str = ""
        probe_service_token: str = ""

    added = _credential_shaped(_Later) - _credential_shaped(Settings)
    assert added == {"probe_api_key", "probe_service_token"}


def test_every_bearer_named_by_a_setting_is_redacted_by_its_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bearer a `*_token_env` setting names is redacted by its value.

    Such settings hold a variable *name*, so they are neither a `SecretStr` nor in
    `_SECRET_SETTINGS`, and the bearer they point at must be registered for redaction separately.
    Driven off `Settings.model_fields`, so a new such setting is covered when declared; asserted on
    the *value*, since a leaked bearer rarely appears beside its variable name.
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
    """The premise: a `SecretStr` hides its value from `repr` and `model_dump`.

    Pydantic behaviour, pinned because if it changes the type buys nothing.
    """
    holder = settings.model_copy(update={"llm_api_key": SecretStr("sk-real-value")})
    assert "sk-real-value" not in repr(holder.llm_api_key)
    assert "sk-real-value" not in str(holder.model_dump())
    assert holder.llm_api_key.get_secret_value() == "sk-real-value"


def test_masking_a_dsn_leaves_a_dsn() -> None:
    """The guard's *positive* property: everything that is not the password survives the mask.

    Asserting only absence would pass a mask that returns `""` or destroys the URL. `model_dump()`
    is where an operator reads which server a failing deployment dialled, so the rest of the DSN
    must survive intact.
    """
    from chemclaw.core.config.dsn import _MASK, mask_dsn

    original = "postgresql://chemclaw:hunter2@db.internal:5433/chemclaw?sslmode=require"

    masked = mask_dsn(original)
    before, after = urlsplit(original), urlsplit(masked)

    assert "hunter2" not in masked
    assert after.password == _MASK
    assert (after.scheme, after.username, after.hostname, after.port) == (
        before.scheme,
        before.username,
        before.hostname,
        before.port,
    ), f"the mask changed something other than the password: {masked}"
    assert (after.path, after.query) == (before.path, before.query)

    # A DSN with no password is returned untouched — no userinfo, and therefore no stray `@`.
    passwordless = "postgresql://db.internal:5432/chemclaw"
    assert mask_dsn(passwordless) == passwordless
    # And so is the libpq keyword spelling, which `core/logging` catches on the path it appears on.
    assert mask_dsn("host=db.internal password=hunter2") == "host=db.internal password=hunter2"


def test_the_envelope_nonce_is_derived_from_the_real_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The envelope nonce is derived from the real secret, not from `**********`.

    `framing_envelope_secret` is the HMAC key the envelope tag is derived from; derived from the
    mask, every deployment would share one nonce, which is what the secret exists to prevent.
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
