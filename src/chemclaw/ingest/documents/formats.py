"""The closed format allowlist, and nothing that can read one.

Manifest validation and the retriever need the extension list in the chat pod, where the document
parsers must not be imported; `parse.py` holds the readers and only the sync worker imports it.
`parse._PARSERS` checks the two halves against each other at import.
"""

# Content types this system can read, keyed by file extension. The walk filters on this before
# reading a byte, which keeps crawling a large share cheap.
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
