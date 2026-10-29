"""Formats this backend claims.

Unstructured's `partition` dispatches on filetype across roughly sixty
extensions; this list is the subset with a real partitioner in the `all-docs`
extra, and it is what `/capability` advertises so a caller can decide *before*
submitting whether this backend is the right one for a file.

Kept as data rather than derived from the library at import time on purpose: the
list must be answerable by `/capability` even when `unstructured` is not
installed yet, which is exactly when a user is deciding whether to install it.
"""

from __future__ import annotations

SUPPORTED_EXTENSIONS: frozenset[str] = frozenset(
    {
        # Portable documents
        ".pdf",
        # Word processing
        ".doc",
        ".docx",
        ".odt",
        ".rtf",
        # Presentations
        ".ppt",
        ".pptx",
        # Spreadsheets
        ".xls",
        ".xlsx",
        ".csv",
        ".tsv",
        # Markup and plain text
        ".txt",
        ".text",
        ".md",
        ".markdown",
        ".rst",
        ".org",
        ".html",
        ".htm",
        ".xml",
        ".json",
        # Email
        ".eml",
        ".msg",
        ".p7s",
        # Ebooks
        ".epub",
        # Images (OCR)
        ".png",
        ".jpg",
        ".jpeg",
        ".tiff",
        ".tif",
        ".bmp",
        ".heic",
        # Archives we expand and parse member-by-member
        ".zip",
        ".tar",
        ".tar.gz",
        ".tgz",
        ".tar.bz2",
        ".tbz2",
        ".tar.xz",
        ".txz",
    }
)

# Extension → the content type we assert to `partition` when the on-disk path
# could not be given the right extension (see `paths.named_view`). Unstructured
# consults an asserted content type *after* positive binary detection but before
# libmagic and the filename, which is the right precedence: the caller's
# extension should beat a guess, not beat a certainty.
#
# Hand-written rather than taken from `mimetypes`, whose table varies by host and
# has no entry at all for `.md`, `.rst`, `.org`, `.tsv` or `.msg` — a dispatch
# that works on one machine and not the next is worse than no fallback.
CONTENT_TYPE_BY_EXTENSION: dict[str, str] = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".rtf": "application/rtf",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".csv": "text/csv",
    ".tsv": "text/tsv",
    ".txt": "text/plain",
    ".text": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".rst": "text/x-rst",
    ".org": "text/org",
    ".html": "text/html",
    ".htm": "text/html",
    ".xml": "application/xml",
    ".json": "application/json",
    ".eml": "message/rfc822",
    ".msg": "application/vnd.ms-outlook",
    ".p7s": "application/pkcs7-signature",
    ".epub": "application/epub+zip",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".tiff": "image/tiff",
    ".tif": "image/tiff",
    ".bmp": "image/bmp",
    ".heic": "image/heic",
}


def content_type_for(extension: str) -> str | None:
    """The content type to assert for an extension, or None to let detection run."""
    return CONTENT_TYPE_BY_EXTENSION.get((extension or "").lower())
