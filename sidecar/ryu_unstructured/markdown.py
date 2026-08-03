"""Element list → markdown.

Unstructured's whole advantage over a plain text extractor is that it returns
*typed* elements — Title, ListItem, Table, CodeSnippet, PageBreak — rather than a
character soup. Flattening those with `"\\n".join(str(e) for e in elements)`
throws away the only thing that makes this backend worth its install cost, and
produces chunks that a retriever cannot tell a heading from a footer in.

So the categories are rendered structurally: titles become headings at their
detected depth, list items become list items, and tables are rebuilt from
`metadata.text_as_html` into a real markdown table.
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from typing import Any

_WS = re.compile(r"[ \t]+")
_BLANK_RUN = re.compile(r"\n{3,}")

# Categories that carry no reader-visible text worth keeping in a retrieval
# corpus. Page numbers and running headers pollute chunks and match nothing.
_DROPPED = {"PageNumber", "Header", "Footer"}

_MAX_HEADING_LEVEL = 6


class _TableParser(HTMLParser):
    """Minimal `<table>` reader — rows of cell strings, nothing else.

    Deliberately stdlib: pulling BeautifulSoup in for one table conversion would
    add a dependency to a package whose install cost is already the main
    complaint against it.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self.header_row_index: int | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._row_has_th = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._row = []
            self._row_has_th = False
        elif tag in ("td", "th"):
            if self._row is None:
                self._row = []
            self._cell = []
            if tag == "th":
                self._row_has_th = True
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._cell is not None:
            text = _WS.sub(" ", "".join(self._cell)).strip()
            # A literal pipe would split the markdown cell it lands in.
            if self._row is not None:
                self._row.append(text.replace("|", "\\|"))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if any(cell for cell in self._row):
                if self._row_has_th and self.header_row_index is None:
                    self.header_row_index = len(self.rows)
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def html_table_to_markdown(raw_html: str) -> str | None:
    """Convert one HTML table to a markdown table, or None if it is unusable."""
    if not raw_html or "<t" not in raw_html.lower():
        return None
    parser = _TableParser()
    try:
        parser.feed(raw_html)
        parser.close()
    except Exception:
        return None
    rows = [row for row in parser.rows if row]
    if not rows:
        return None

    width = max(len(row) for row in rows)
    padded = [row + [""] * (width - len(row)) for row in rows]

    # A markdown table cannot be headerless — the delimiter row is mandatory — so
    # the first row is promoted when the HTML marks no header at all, which is the
    # usual case for an extracted table (Unstructured's `text_as_html` emits every
    # cell as <td>). The one case we refuse to guess at is a <th> row partway down:
    # reordering rows would silently rewrite the document, so that table gets a
    # blank header and keeps every row in place.
    if parser.header_row_index in (0, None):
        header, body = padded[0], padded[1:]
    else:
        header, body = [""] * width, padded

    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(["---"] * width) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in body)
    return "\n".join(lines)


def _heading(text: str, depth: Any) -> str:
    level = 1
    if isinstance(depth, int) and depth >= 0:
        level = min(depth + 1, _MAX_HEADING_LEVEL)
    return f"{'#' * level} {text}"


def _element_metadata(element: Any) -> dict[str, Any]:
    meta = getattr(element, "metadata", None)
    if meta is None:
        return {}
    to_dict = getattr(meta, "to_dict", None)
    if callable(to_dict):
        try:
            return to_dict() or {}
        except Exception:
            return {}
    return dict(getattr(meta, "__dict__", {}) or {})


def element_to_record(element: Any) -> dict[str, Any]:
    """Normalise one Unstructured element into a JSON-safe record."""
    meta = _element_metadata(element)
    return {
        "id": str(getattr(element, "id", "") or ""),
        "category": str(getattr(element, "category", "") or type(element).__name__),
        "text": str(element) or "",
        "page_number": meta.get("page_number"),
        "languages": meta.get("languages"),
        "filename": meta.get("filename"),
        "parent_id": meta.get("parent_id"),
        "text_as_html": meta.get("text_as_html"),
        "category_depth": meta.get("category_depth"),
    }


def _render_record(record: dict[str, Any]) -> str | None:
    category = record["category"]
    text = (record["text"] or "").strip()

    if category == "Table":
        table = html_table_to_markdown(record.get("text_as_html") or "")
        if table:
            return table
        # No HTML form (the `fast` strategy does not emit one): keep the plain
        # text rather than dropping the table entirely.
        return text or None

    if category == "PageBreak":
        page = record.get("page_number")
        return "\n---\n" if page is None else f"\n---\n<!-- page {page} -->\n"

    if not text:
        return None
    if category in _DROPPED:
        return None
    if category in ("Title", "Headline", "Subtitle", "SectionHeader"):
        return _heading(text, record.get("category_depth"))
    if category == "ListItem":
        # Multi-line list items keep their continuation indented under the bullet.
        first, *rest = text.splitlines()
        item = f"- {first.strip()}"
        for line in rest:
            item += f"\n  {line.strip()}"
        return item
    if category == "CodeSnippet":
        return f"```\n{text}\n```"
    if category == "Formula":
        return f"$$\n{text}\n$$"
    if category in ("FigureCaption", "Caption"):
        return f"*{text}*"
    if category == "Image":
        return f"*[image] {text}*"
    if category == "Address":
        return text
    return text


def records_to_markdown(records: list[dict[str, Any]]) -> str:
    """Render normalised element records as markdown.

    Consecutive list items are kept adjacent (one blank line between blocks
    everywhere else) so a list survives as a list through chunking.
    """
    blocks: list[str] = []
    previous_was_list = False
    for record in records:
        rendered = _render_record(record)
        if not rendered:
            continue
        is_list = rendered.startswith("- ")
        if blocks and not (is_list and previous_was_list):
            blocks.append("")
        blocks.append(rendered)
        previous_was_list = is_list
    return _BLANK_RUN.sub("\n\n", "\n".join(blocks)).strip()


def records_to_text(records: list[dict[str, Any]]) -> str:
    """Plain-text rendering, for callers that want no markup at all."""
    parts = [
        html.unescape(record["text"]).strip()
        for record in records
        if record["text"] and record["text"].strip() and record["category"] not in _DROPPED
    ]
    return "\n\n".join(parts)
