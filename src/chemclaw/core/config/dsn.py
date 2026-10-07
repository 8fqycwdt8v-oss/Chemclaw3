"""The annotated `str` a database DSN is declared as, so its password is not a `repr` away.

A libpq URL carries its password in the userinfo, so a plain `str` DSN leaks through `repr`, `str`
and `model_dump`. The value stays a `str` so every dial site keeps working (no
`.get_secret_value()`); only renderings are masked. Two mechanisms, because pydantic renders
`repr`/`str` from field values and dumps from serializers: `repr=False` closes the first and a
`PlainSerializer` the second. The mask keeps the host, since "which server did it try" is what an
operator needs to diagnose.
"""

from typing import Annotated
from urllib.parse import urlsplit, urlunsplit

from pydantic import Field, PlainSerializer

_MASK = "***"


def mask_dsn(dsn: str) -> str:
    """`dsn` with the userinfo password replaced by `***`, and everything else intact.

    Returns the raw value when there is no URL password: the empty default and the libpq keyword
    form (`host=… password=…`), which `core/logging`'s `PASSWORD=` rule masks instead.
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
