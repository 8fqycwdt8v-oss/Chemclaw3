"""The closed format allowlist, and nothing that can read one.

Split out of `parse.py` for the reason `connectors/calc/specs.py` was split out of its results
(D-118): a manifest binding has to *validate* an extension list, and a retriever has to know that
`.pdf` is a document, long before anything wants to open one. Both of those run in the chat pod,
where `pypdf`, `python-docx`, `python-pptx` and `openpyxl` have no business being imported.

So this module names the formats and imports nothing. `parse.py` is what can read them, and only
the sync worker imports that.

Adding a format is one entry here plus its parser in `parse.py` — the two halves are checked
against each other at import time by `parse._PARSERS`, so a format named here with no parser is a
loud failure rather than a file type that silently never matches.
"""

# Content types this system can read, mapped from the extension a file share actually carries.
# A share is full of things that are not documents (images, CAD, archives, executables); the walk
# filters on this map before a single byte is read, which is what makes crawling a TB share cheap.
EXTENSIONS: dict[str, str] = {
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".pdf": "application/pdf",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

SUPPORTED_EXTENSIONS = frozenset(EXTENSIONS)
SUPPORTED_CONTENT_TYPES = frozenset(EXTENSIONS.values())


def content_type_for(name: str, declared: str | None = None) -> str:
    """Resolve a content type from the declared value, falling back to the file extension.

    Args:
        name: The file name, used for its extension when the declared type is absent or unreadable.
        declared: A client-supplied content type, if any (parameters like `; charset=` are dropped).

    Returns:
        A supported content type, or the declared/unknown one so the caller can refuse it by name.
    """
    if declared:
        base = declared.split(";")[0].strip().lower()
        if base in SUPPORTED_CONTENT_TYPES:
            return base
    for suffix, content_type in EXTENSIONS.items():
        if name.lower().endswith(suffix):
            return content_type
    return (declared or "application/octet-stream").split(";")[0].strip().lower()
