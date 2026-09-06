"""The annotated `str` a database DSN is declared as, so its password is not a `repr` away.

**A type is where this belongs, because the leak was a type.** Five of this config's credentials
are `SecretStr` and are masked everywhere. The three DSNs — `postgres_dsn`,
`postgres_migration_dsn`, `session_store_dsn` — were plain `str`, and a libpq URL carries its
password in the userinfo, so `repr(settings)`, `str(settings)`, `model_dump()` and
`model_dump_json()` each disclosed all three. Measured with marker passwords: four renderings,
three leaks each.

**Why that was easy to miss for as long as it was.** The *log* path is fully defended and was
measured so — `repr(settings)` through the configured handler comes out clean, because
`core/logging._SECRET_SETTINGS` names all three and `_URL_USERINFO` catches the shape
independently. So the one place anyone looks was already right, and the residual is everything
that is not a `LogRecord`: a `print`, a debugger, a crash reporter, a dump written to a file or a
response, an exported span. The strength of one control is what hid the absence of the other.

**Why not `SecretStr`.** There is no `SecretStr` DSN that keeps `psycopg` happy without an
`.get_secret_value()` at every dial site, and adding one to each of those sites is a change to code
whose only defect is what a *rendering* of it says. So the value stays an ordinary `str` — attribute
access is untouched, and every caller that dials keeps working unchanged — and only the renderings
are masked: `repr=False` on the field closes `repr`/`str`, and a `PlainSerializer` closes
`model_dump`/`model_dump_json`.

**Two mechanisms, because they close different sinks and neither closes both.** Pydantic renders
a model's `repr`/`str` from the field *values* and its dumps from the *serializers*, so
`PlainSerializer` alone leaves `repr(settings)` intact and `repr=False` alone leaves
`model_dump_json()` intact. `repr=False` drops the field from `repr`/`str` entirely — host and all,
which is the one thing lost here: an operator reading a bare `repr(settings)` no longer sees which
server the DSN names, and `model_dump()` is where they get it, masked. That trade is the right way
round for a rendering that reaches a crash reporter.

**The host survives the *mask*, deliberately.** The failure this config is diagnosed for is "which
server did it try", so masking the whole DSN would trade a disclosure for an outage nobody can
read. Only the userinfo password is replaced.
"""

from typing import Annotated
from urllib.parse import urlsplit, urlunsplit

from pydantic import Field, PlainSerializer

_MASK = "***"


def mask_dsn(dsn: str) -> str:
    """`dsn` with the userinfo password replaced by `***`, and everything else intact.

    Falls back to the raw value only when there is nothing that looks like a URL password, which
    covers the two other spellings this config accepts: the empty default, and the libpq
    `host=… password=…` keyword form. The keyword form is *not* masked here on purpose — it has
    never been the shipped spelling for these three fields, and a second parser in a serializer
    would be a place for the two to disagree. `core/logging`'s structural `PASSWORD=` rule is what
    catches that spelling, on the path where it has actually been seen.
    """
    if "://" not in dsn:
        return dsn
    parts = urlsplit(dsn)
    if not parts.password:
        return dsn
    userinfo = f"{parts.username or ''}:{_MASK}@"
    host = parts.hostname or ""
    netloc = f"{userinfo}{host}" + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


DatabaseDsn = Annotated[
    str,
    Field(repr=False),
    PlainSerializer(mask_dsn, return_type=str, when_used="always"),
]
"""A Postgres DSN: an ordinary `str` to every caller, masked in every rendering."""
