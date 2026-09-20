"""The parse itself: a file path in, a `document.parse` result out.

The path and the *name* are two different things and both matter. Core's primary
submit form points at `~/.ryu/blobs/<shard>/<sha256>` — a content-addressed blob
with no extension — and carries the document's real name in a separate
`filename` field. Every format decision here therefore keys off the display
name, never `path.suffix`: dispatching on the blob path would make
`missing_required` return nothing for a `.doc`, skip archive expansion for a
`.zip`, and leave `partition` sniffing a ZIP container it cannot tell from four
other formats.

Everything that can go wrong here is reported as a typed error the job carries,
never as an exception that kills the worker or an empty result that looks like a
blank document:

  * `library_missing`      — `unstructured` was never installed
  * `missing_dependency`   — a native binary this format needs is absent
  * `unsupported_format`   — the library has no partitioner for this extension
  * `parse_failed`         — the partitioner raised
  * `input_rejected`       — path confinement / archive safety refused the input
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from . import BACKEND
from .deps import missing_optional, missing_required, unstructured_version
from .formats import content_type_for
from .limits import MAX_OUTPUT_BYTES
from .markdown import element_to_record, records_to_markdown, records_to_text
from .paths import InputError, extension_of, is_archive_name, named_view, safe_extract


class ParseError(RuntimeError):
    """A parse failure with a machine-readable code and a human-readable fix."""

    def __init__(self, code: str, message: str, *, missing: list[str] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.missing = missing or []


def _truncate(text: str, budget: int) -> tuple[str, bool]:
    """Clip to a byte budget on a character boundary."""
    encoded = text.encode("utf-8")
    if len(encoded) <= budget:
        return text, True
    clipped = encoded[:budget].decode("utf-8", errors="ignore")
    return clipped, False


def _partition(path: Path, name: str, options: dict[str, Any]) -> list[Any]:
    try:
        from unstructured.partition.auto import partition
    except ImportError as exc:
        raise ParseError(
            "library_missing",
            "the `unstructured` library is not installed in this sidecar's venv — "
            'install it with `pip install "unstructured[all-docs]"`',
        ) from exc

    kwargs: dict[str, Any] = {}
    # `strategy` is the single knob worth exposing: `fast` skips layout detection
    # and OCR entirely (seconds instead of minutes on a born-digital PDF),
    # `hi_res` runs the layout model, `ocr_only` forces Tesseract.
    strategy = options.get("strategy")
    if isinstance(strategy, str) and strategy in ("auto", "fast", "hi_res", "ocr_only"):
        kwargs["strategy"] = strategy
    languages = options.get("languages")
    if isinstance(languages, list) and all(isinstance(lang, str) for lang in languages):
        kwargs["languages"] = languages
    if options.get("infer_table_structure") is not None:
        kwargs["infer_table_structure"] = bool(options["infer_table_structure"])

    try:
        # `named_view` lends the blob an extension for the dispatch; every message
        # below still names the *document*, never the scratch link, so an error
        # the user reads says `Q3 report.pdf` and not `input.pdf`.
        with named_view(path, name) as dispatch_path:
            if extension_of(dispatch_path.name) != extension_of(name):
                # No link could be made, so the path still carries the wrong (or
                # no) extension. Assert the content type instead — weaker, since
                # a handful of formats have no unambiguous one, but far better
                # than letting a `.txt` blob detect as FileType.UNK.
                asserted = content_type_for(extension_of(name))
                if asserted:
                    kwargs["content_type"] = asserted
            return list(partition(filename=str(dispatch_path), **kwargs))
    except ImportError as exc:
        # `partition` lazily imports the per-format extra, so a missing
        # `unstructured[docx]` lands here rather than at module import.
        raise ParseError(
            "unsupported_format",
            f"`{extension_of(name) or name}` needs an Unstructured extra that is not "
            f'installed: {exc}. Install it with `pip install "unstructured[all-docs]"`.',
        ) from exc
    except FileNotFoundError as exc:
        # A converter shelling out to a binary that is not on PATH. The preflight
        # catches the ones we know about; this catches the rest with the same
        # actionable shape instead of a bare traceback.
        raise ParseError(
            "missing_dependency",
            f"a native tool required to parse `{name}` is not installed: {exc}",
        ) from exc
    except Exception as exc:
        raise ParseError(
            "parse_failed", f"parsing `{name}` failed: {type(exc).__name__}: {exc}"
        ) from exc


def _preflight(name: str) -> list[str]:
    """Refuse formats whose native tool is absent; return degradation warnings.

    This is the whole point of the backend's error reporting: `.doc` without
    LibreOffice must come back as "libreoffice is not installed — …", by name and
    with the install command, rather than as a document that mysteriously has no
    text in it.
    """
    ext = extension_of(name)
    required = missing_required(ext)
    if required:
        raise ParseError(
            "missing_dependency",
            " ".join(dep.message() for dep in required),
            missing=[dep.key for dep in required],
        )
    return [dep.message() for dep in missing_optional(ext)]


def _parse_one(
    path: Path, name: str, options: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[str]]:
    warnings = _preflight(name)
    elements = _partition(path, name, options)
    return [element_to_record(element) for element in elements], warnings


def parse_file(
    path: Path,
    options: dict[str, Any] | None = None,
    display_name: str | None = None,
) -> dict[str, Any]:
    """Parse one file (or one archive of files) into a `document.parse` result.

    `display_name` is the document's real name (Core's `filename` field);
    `path` is only where the bytes are. Format dispatch, dependency preflight and
    every message key off the former — see the module docstring.

    The shape is the contract's: `markdown` is the primary payload, `text` the
    markup-free fallback, `elements` the typed detail only this backend can
    supply, and `truncated` says whether the byte budget clipped the output.
    """
    options = options or {}
    name = display_name or path.name
    if is_archive_name(name):
        records, warnings, sources = _parse_archive(path, name, options)
    else:
        records, warnings = _parse_one(path, name, options)
        sources = [name]

    markdown, whole_md = _truncate(records_to_markdown(records), MAX_OUTPUT_BYTES)
    text, whole_text = _truncate(records_to_text(records), MAX_OUTPUT_BYTES)
    pages = sorted(
        {
            record["page_number"]
            for record in records
            if isinstance(record.get("page_number"), int)
        }
    )
    return {
        "backend": BACKEND,
        "backend_version": unstructured_version(),
        "markdown": markdown,
        "text": text,
        "elements": records,
        "warnings": warnings,
        "truncated": not (whole_md and whole_text),
        "metadata": {
            "filename": name,
            "element_count": len(records),
            "page_count": len(pages) or None,
            "sources": sources,
        },
    }


def _parse_archive(
    path: Path, name: str, options: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Expand an archive into a scratch dir and parse every member we can read.

    One unreadable member must not sink the whole archive, so per-member failures
    become warnings and the rest of the documents still come back.
    """
    records: list[dict[str, Any]] = []
    warnings: list[str] = []
    sources: list[str] = []
    with tempfile.TemporaryDirectory(prefix="ryu-unstructured-") as scratch:
        try:
            members = safe_extract(path, Path(scratch))
        except InputError as exc:
            raise ParseError("input_rejected", f"`{name}`: {exc}") from exc
        for member in sorted(members):
            relative = str(member.relative_to(Path(scratch).resolve()))
            try:
                # An expanded member is a real file with a real name, so its own
                # path is the display name here.
                member_records, member_warnings = _parse_one(member, relative, options)
            except ParseError as exc:
                warnings.append(f"{relative}: {exc}")
                continue
            if not member_records:
                continue
            sources.append(relative)
            warnings.extend(f"{relative}: {warning}" for warning in member_warnings)
            # Push every heading in the member one level down so the member's own
            # structure nests under its filename heading instead of competing with
            # it — otherwise an archive renders as a flat run of `#` headings with
            # no way to tell where one document ends.
            for record in member_records:
                depth = record.get("category_depth")
                record["category_depth"] = depth + 1 if isinstance(depth, int) else 1
            records.append(
                {
                    "id": "",
                    "category": "Title",
                    "text": relative,
                    "page_number": None,
                    "languages": None,
                    "filename": relative,
                    "parent_id": None,
                    "text_as_html": None,
                    "category_depth": 0,
                }
            )
            records.extend(member_records)
    return records, warnings, sources
