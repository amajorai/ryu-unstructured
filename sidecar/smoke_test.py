"""Smoke test: the contract holds with or without the parser library installed.

Covers the acceptance criteria that need no native tooling:
  - an unauthenticated request is rejected (fail-closed bearer gate)
  - GET /health is open, POST /health is NOT
  - /capability answers even when `unstructured` is absent
  - POST /parse returns 202 + a job_id immediately, and the job reaches a
    terminal state (succeeded with the library installed, `library_missing`
    without it — either way, never a hang and never a crash)
  - path confinement rejects `..`, absolute paths outside the roots, and
    symlinks pointing out of the allowed roots
  - archive expansion rejects traversal members
  - the BLOB form (an extensionless content-addressed path + a separate
    `filename`) dispatches on the filename: the job reports the document's real
    name, the native-tool preflight fires, and an archive still goes through
    `safe_extract`

A real `.docx`/`.pdf` parse needs `pip install "unstructured[all-docs]"` plus the
native tools and is out of scope here; the test prints which mode it ran in.
"""

from __future__ import annotations

import os
import tempfile
import time
import zipfile
from pathlib import Path

# The server reads RYU_EXT_TOKEN at import time for its fail-closed auth gate;
# set it before importing `app` and present it as the bearer on every request.
os.environ.setdefault("RYU_EXT_TOKEN", "smoke-token")

# Confine parse inputs to this run's scratch dir so the confinement test has a
# real boundary to cross. Must also be set before the modules read it.
_SCRATCH = Path(tempfile.mkdtemp(prefix="ryu-unstructured-smoke-"))
_ROOT = _SCRATCH / "root"
_ROOT.mkdir()
os.environ["RYU_UNSTRUCTURED_ROOTS"] = str(_ROOT)
os.environ["RYU_UNSTRUCTURED_WORKDIR"] = str(_SCRATCH / "work")

from fastapi.testclient import TestClient  # noqa: E402

from ryu_unstructured.deps import LIBREOFFICE  # noqa: E402
from ryu_unstructured.markdown import html_table_to_markdown, records_to_markdown  # noqa: E402
from ryu_unstructured.paths import (  # noqa: E402
    InputError,
    extension_of,
    is_archive_name,
    safe_basename,
    safe_extract,
)
from ryu_unstructured.server import app  # noqa: E402

TOKEN = os.environ["RYU_EXT_TOKEN"]
client = TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"})
anon = TestClient(app)

TERMINAL = {"succeeded", "failed", "cancelled"}


def _await_terminal(job_id: str, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    snap: dict = {}
    while time.time() < deadline:
        snap = client.get(f"/jobs/{job_id}").json()
        if snap["status"] in TERMINAL:
            return snap
        time.sleep(0.2)
    raise AssertionError(f"job {job_id} never reached a terminal state: {snap}")


def test_auth_is_fail_closed() -> None:
    assert anon.post("/parse", json={"path": "/x"}).status_code == 401
    assert anon.get("/jobs").status_code == 401
    assert anon.get("/capability").status_code == 401
    # /health is exempt on GET only.
    assert anon.get("/health").status_code == 200
    assert anon.post("/health").status_code == 401
    print("auth: unauthenticated rejected, GET /health open, POST /health closed")


def test_capability_answers_without_the_library() -> None:
    cap = client.get("/capability").json()
    assert cap["capability"] == "document.parse", cap
    assert cap["backend"] == "unstructured", cap
    assert ".pdf" in cap["formats"] and ".docx" in cap["formats"], cap
    assert cap["limits"]["timeout_secs"] > 0, cap
    print(
        f"capability: available={cap['available']} "
        f"library={cap['library_version']} missing={cap['missing_dependencies']}"
    )
    return cap


def test_parse_roundtrip(available: bool) -> None:
    fixture = _ROOT / "hello.txt"
    fixture.write_text(
        "Ryu Document Parsing\n\nUnstructured turns typed elements into markdown.\n",
        encoding="utf-8",
    )
    submitted = client.post("/parse", json={"path": str(fixture)})
    assert submitted.status_code == 202, submitted.text
    job_id = submitted.json()["job_id"]
    assert job_id, submitted.text

    snap = _await_terminal(job_id)
    if available:
        assert snap["status"] == "succeeded", snap
        result = snap["result"]
        assert "Unstructured" in result["markdown"], result["markdown"][:200]
        assert result["elements"], result
        assert result["backend"] == "unstructured", result
        print(
            f"parse: succeeded, {result['metadata']['element_count']} elements, "
            f"{len(result['markdown'])} md chars"
        )
    else:
        assert snap["status"] == "failed", snap
        assert snap["error_code"] == "library_missing", snap
        print(f"parse: library absent, clean job error -> {snap['error'][:80]}...")


def test_inline_parse_accepted() -> None:
    import base64

    body = base64.b64encode(b"inline body\n").decode("ascii")
    r = client.post("/parse", json={"content_base64": body, "filename": "note.txt"})
    assert r.status_code == 202, r.text
    snap = _await_terminal(r.json()["job_id"])
    # Not the mkstemp scratch name the bytes were written to.
    assert snap["filename"] == "note.txt", snap
    print("inline: content_base64 accepted, reported under its own filename")


def test_path_confinement() -> None:
    outside = _SCRATCH / "outside.txt"
    outside.write_text("secret", encoding="utf-8")

    rejected = client.post("/parse", json={"path": str(outside)})
    assert rejected.status_code == 400, rejected.text
    assert rejected.json()["error_code"] == "input_rejected", rejected.text

    traversal = client.post("/parse", json={"path": f"{_ROOT}/../outside.txt"})
    assert traversal.status_code == 400, traversal.text

    link = _ROOT / "link.txt"
    link.symlink_to(outside)
    escaped = client.post("/parse", json={"path": str(link)})
    assert escaped.status_code == 400, escaped.text

    both = client.post("/parse", json={})
    assert both.status_code == 400, both.text
    print("confinement: outside-root, `..`, and escaping symlink all rejected")


def test_archive_traversal_rejected() -> None:
    bomb = _ROOT / "evil.zip"
    with zipfile.ZipFile(bomb, "w") as zf:
        zf.writestr("../escaped.txt", "pwned")
    try:
        safe_extract(bomb, _SCRATCH / "extract")
        raise AssertionError("traversal member was extracted")
    except InputError as exc:
        assert "parent-directory" in str(exc), exc

    absolute = _ROOT / "absolute.zip"
    with zipfile.ZipFile(absolute, "w") as zf:
        zf.writestr("/etc/passwd", "pwned")
    try:
        safe_extract(absolute, _SCRATCH / "extract2")
        raise AssertionError("absolute member was extracted")
    except InputError as exc:
        assert "absolute" in str(exc), exc
    print("archive: `..` and absolute members rejected")


def _blob(contents: bytes, digest: str) -> Path:
    """A file shaped like one of Core's content-addressed blobs: no extension."""
    shard = _ROOT / "blobs" / digest[:2]
    shard.mkdir(parents=True, exist_ok=True)
    target = shard / digest
    target.write_bytes(contents)
    return target


def test_name_derivation() -> None:
    # `.tar.gz`'s suffix is `.gz`, which is why archive detection matches on the
    # whole name and only the dependency lookup uses the final extension.
    assert is_archive_name("report.tar.gz") and is_archive_name("a.ZIP"), "archive names"
    assert not is_archive_name("report.pdf"), "pdf is not an archive"
    assert extension_of("X.DOC") == ".doc", extension_of("X.DOC")
    assert extension_of("report.tar.gz") == ".gz", extension_of("report.tar.gz")
    assert extension_of("9f2c4ab1") == "", "a blob address has no extension"
    # A filename reaches the desktop chip, so only the last segment survives.
    assert safe_basename("../../etc/passwd") == "passwd", safe_basename("../../etc/passwd")
    assert safe_basename("C:\\Users\\x\\Q3.pdf") == "Q3.pdf"
    assert safe_basename("   ") is None and safe_basename("..") is None
    print("names: archive/extension/basename derivation all key off the display name")


def test_blob_form_uses_original_filename(available: bool) -> None:
    """The primary form: an extensionless blob path + the document's real name."""
    digest = "a1" + "b2c3d4e5" * 7 + "f60912"
    blob = _blob(b"Ryu Document Parsing\n\nBlob-form dispatch keeps the name.\n", digest)
    assert blob.suffix == "", "fixture must have no extension"

    submitted = client.post(
        "/parse",
        json={
            "path": str(blob),
            "blob_sha256": digest,
            "filename": "Quarterly notes.txt",
            "mime": "text/plain",
            "size_bytes": blob.stat().st_size,
        },
    )
    assert submitted.status_code == 202, submitted.text
    snap = _await_terminal(submitted.json()["job_id"])
    # The chip must read `Quarterly notes.txt`, never 64 hex characters.
    assert snap["filename"] == "Quarterly notes.txt", snap
    if available:
        assert snap["status"] == "succeeded", snap
        assert snap["result"]["metadata"]["filename"] == "Quarterly notes.txt", snap
    print(f"blob form: reported as `{snap['filename']}` (status {snap['status']})")


def test_blob_form_rejects_a_steering_filename() -> None:
    digest = "cc" + "0" * 62
    blob = _blob(b"contained\n", digest)
    submitted = client.post(
        "/parse", json={"path": str(blob), "filename": "../../etc/passwd"}
    )
    assert submitted.status_code == 202, submitted.text
    snap = _await_terminal(submitted.json()["job_id"])
    assert snap["filename"] == "passwd", snap
    print("blob form: a traversal `filename` is reduced to its basename")


def test_blob_form_preflights_native_tools() -> None:
    """`libreoffice is not installed` must reach the UI, on the blob form too.

    This is the case that dispatching on `path.suffix` silently lost: the blob has
    no extension, so `REQUIRED_BY_EXT` was never consulted and a `.doc` came back
    as an empty document instead of an actionable error.
    """
    digest = ("de" + "adbeef" * 11)[:64]
    blob = _blob(b"\xd0\xcf\x11\xe0legacy word binary", digest)
    submitted = client.post(
        "/parse", json={"path": str(blob), "filename": "legacy.doc"}
    )
    assert submitted.status_code == 202, submitted.text
    snap = _await_terminal(submitted.json()["job_id"])
    assert snap["filename"] == "legacy.doc", snap
    if LIBREOFFICE.present():
        print("preflight: libreoffice IS installed here — dependency error not exercised")
        return
    assert snap["status"] == "failed", snap
    assert snap["error_code"] == "missing_dependency", snap
    assert snap["missing_dependencies"] == ["libreoffice"], snap
    assert "libreoffice is not installed" in snap["error"], snap["error"]
    print(f"preflight: blob-form .doc -> {snap['error'][:70]}...")


def test_blob_form_expands_archives_safely() -> None:
    """A `.zip` submitted as an extensionless blob still meets `safe_extract`."""
    digest = "ff" + "1" * 62
    shard = _ROOT / "blobs" / digest[:2]
    shard.mkdir(parents=True, exist_ok=True)
    blob = shard / digest
    with zipfile.ZipFile(blob, "w") as zf:
        zf.writestr("../escaped.txt", "pwned")

    submitted = client.post("/parse", json={"path": str(blob), "filename": "evil.zip"})
    assert submitted.status_code == 202, submitted.text
    snap = _await_terminal(submitted.json()["job_id"])
    assert snap["status"] == "failed", snap
    assert snap["error_code"] == "input_rejected", snap
    assert "parent-directory" in snap["error"], snap["error"]
    print("blob form: an extensionless zip still goes through archive containment")


def test_markdown_shaping() -> None:
    table = html_table_to_markdown(
        "<table><tr><th>Region</th><th>Total</th></tr>"
        "<tr><td>EU</td><td>12</td></tr></table>"
    )
    assert table is not None and table.splitlines()[0] == "| Region | Total |", table

    md = records_to_markdown(
        [
            {"category": "Title", "text": "Heading", "category_depth": 0, "text_as_html": None},
            {"category": "ListItem", "text": "first", "category_depth": None, "text_as_html": None},
            {"category": "ListItem", "text": "second", "category_depth": None, "text_as_html": None},
            {"category": "PageNumber", "text": "3", "category_depth": None, "text_as_html": None},
        ]
    )
    assert md.startswith("# Heading"), md
    assert "- first\n- second" in md, md
    assert "3" not in md, md
    print("markdown: titles->headings, list runs stay adjacent, page numbers dropped")


def main() -> None:
    test_auth_is_fail_closed()
    cap = test_capability_answers_without_the_library()
    test_parse_roundtrip(bool(cap["available"]))
    test_inline_parse_accepted()
    test_path_confinement()
    test_archive_traversal_rejected()
    test_name_derivation()
    test_blob_form_uses_original_filename(bool(cap["available"]))
    test_blob_form_rejects_a_steering_filename()
    test_blob_form_preflights_native_tools()
    test_blob_form_expands_archives_safely()
    test_markdown_shaping()
    mode = "with unstructured installed" if cap["available"] else "without unstructured"
    print(f"\nSMOKE_OK ({mode})")


if __name__ == "__main__":
    main()
