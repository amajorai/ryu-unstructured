"""Ryu Unstructured sidecar — HTTP front for the `document.parse` capability.

The contract Core drives (every path below is declared in `manifest.json`; an
undeclared path 404s at the ext-proxy before it ever reaches this process):

    GET    /health          -> { ok, backend, available, missing_dependencies }
    GET    /capability      -> { backend, formats, limits, system_dependencies }
    POST   /parse           -> 202 { job_id, status }        (never blocks)
    GET    /jobs            -> { jobs: [ JobSnapshot ] }      (no results)
    GET    /jobs/{job_id}   -> JobSnapshot                    (result when done)
    DELETE /jobs/{job_id}   -> JobSnapshot                    (cooperative cancel)

Submit-then-poll is not a style choice: the ext-proxy's activity guard drops when
response headers arrive, so a single long-lived parse request on a `lazy` +
`idle_stop_secs` sidecar can be reaped mid-flight. Polling re-arms the guard.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import BACKEND, __version__
from .deps import snapshot
from .formats import SUPPORTED_EXTENSIONS
from .jobs import STORE
from .limits import (
    MAX_INPUT_BYTES,
    MAX_JOBS,
    MAX_OUTPUT_BYTES,
    MAX_WORKERS,
    TIMEOUT_SECS,
)
from .paths import InputError, resolve_input, safe_basename, workdir

app = FastAPI(title="Ryu Unstructured Sidecar", version=__version__)

# Shared-secret bearer Core stamps on every proxied hop and injects at spawn
# (`RYU_EXT_TOKEN`). FAIL-CLOSED for every route except GET /health: no token
# configured => reject all. Without it, any local process (or any web page that
# can reach loopback) could hand this sidecar a path and read the file back as
# "parsed text" — an arbitrary-file-read primitive.
_EXPECTED_TOKEN = (os.environ.get("RYU_EXT_TOKEN") or "").strip()


@app.middleware("http")
async def _require_ext_token(request: Request, call_next):
    # GET only: a POST to /health must not become an unauthenticated hole if the
    # route ever grows a body.
    if request.url.path == "/health" and request.method == "GET":
        return await call_next(request)
    header = request.headers.get("authorization", "")
    presented = header[len("Bearer ") :].strip() if header.startswith("Bearer ") else ""
    if not (_EXPECTED_TOKEN and hmac.compare_digest(presented, _EXPECTED_TOKEN)):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await call_next(request)


class ParseRequest(BaseModel):
    path: Optional[str] = Field(
        None,
        description="Absolute path to the document, confined to RYU_UNSTRUCTURED_ROOTS.",
    )
    content_base64: Optional[str] = Field(
        None, description="Inline document bytes, for callers with no shared filesystem."
    )
    filename: Optional[str] = Field(
        None,
        description="The document's ORIGINAL name. Load-bearing on BOTH forms: the "
        "`path` form points at a content-addressed blob with no extension, and the "
        "extension is what selects the partitioner and the native-tool preflight.",
    )
    blob_sha256: Optional[str] = Field(
        None,
        description="Advisory. The blob's address, for the caller's own integrity "
        "and caching. This sidecar never reconstructs a path from it — Core owns "
        "the blob layout so a future non-filesystem store stays Core's business.",
    )
    mime: Optional[str] = Field(
        None, description="Advisory only; the extension of `filename` wins."
    )
    size_bytes: Optional[int] = Field(
        None, description="Advisory only; the file is re-stat'd under the input cap."
    )
    options: dict[str, Any] = Field(
        default_factory=dict,
        description="Backend hints: strategy (auto|fast|hi_res|ocr_only), languages, "
        "infer_table_structure. Unknown keys are ignored, never an error — a "
        "hint one backend understands must not fail on another.",
    )


def _limits() -> dict[str, Any]:
    return {
        "max_input_bytes": MAX_INPUT_BYTES,
        "max_output_bytes": MAX_OUTPUT_BYTES,
        "timeout_secs": TIMEOUT_SECS,
        "max_workers": MAX_WORKERS,
        "max_jobs": MAX_JOBS,
    }


@app.get("/health")
def health() -> dict[str, Any]:
    probe = snapshot()
    return {
        "ok": True,
        "version": __version__,
        "backend": BACKEND,
        # `available` is the honest answer to "can this parse anything right
        # now": the library must be importable. Missing *system* deps narrow the
        # format list rather than disabling the backend.
        "available": bool(probe["library_available"]),
        "library_version": probe["library_version"],
        "missing_dependencies": probe["missing_system_dependencies"],
    }


@app.get("/capability")
def capability() -> dict[str, Any]:
    probe = snapshot()
    return {
        "capability": "document.parse",
        "backend": BACKEND,
        "version": __version__,
        "available": bool(probe["library_available"]),
        "library_version": probe["library_version"],
        "libmagic": probe["libmagic"],
        "formats": sorted(SUPPORTED_EXTENSIONS),
        "system_dependencies": probe["system_dependencies"],
        "missing_dependencies": probe["missing_system_dependencies"],
        "limits": _limits(),
    }


def _staging_dir() -> Path:
    """Where inline uploads land. Core points this at `${RYU_DIR}/cache/...`."""
    staging = workdir() / "uploads"
    staging.mkdir(parents=True, exist_ok=True)
    return staging


def _materialise_inline(req: ParseRequest) -> Path:
    """Write `content_base64` to a scratch file we own, returning its path."""
    try:
        raw = base64.b64decode(req.content_base64 or "", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InputError(f"`content_base64` is not valid base64: {exc}") from exc
    if not raw:
        raise InputError("`content_base64` decoded to zero bytes")
    if len(raw) > MAX_INPUT_BYTES:
        raise InputError(
            f"inline input is {len(raw)} bytes, over the {MAX_INPUT_BYTES}-byte limit"
        )
    # Only the extension is taken from the caller's filename — the rest of the
    # name is ours, so a crafted `filename` cannot steer the write anywhere.
    suffix = Path(safe_basename(req.filename) or "document.txt").suffix[:16]
    handle, tmp_path = tempfile.mkstemp(suffix=suffix or ".txt", dir=str(_staging_dir()))
    with os.fdopen(handle, "wb") as dst:
        dst.write(raw)
    return Path(tmp_path)


@app.post("/parse")
def parse(req: ParseRequest) -> JSONResponse:
    if bool(req.path) == bool(req.content_base64):
        return JSONResponse(
            {"error": "provide exactly one of `path` or `content_base64`"},
            status_code=400,
        )
    try:
        target = _materialise_inline(req) if req.content_base64 else resolve_input(req.path or "")
    except InputError as exc:
        return JSONResponse(
            {"error": str(exc), "error_code": "input_rejected"}, status_code=400
        )

    # The name the job reports and dispatches on. It is *not* `target.name`: the
    # blob form has no extension, and the inline form's target is an mkstemp
    # scratch file whose name is random. Basename-only — this string is echoed
    # back on every poll and rendered as the desktop's attachment chip.
    display_name = safe_basename(req.filename) or target.name
    job = STORE.submit(target, req.options or {}, display_name)
    # 202: the parse has been accepted, not performed. The caller polls
    # /jobs/{job_id} — see the module docstring for why this may not be one
    # long request.
    return JSONResponse({"job_id": job.id, "status": job.status}, status_code=202)


@app.get("/jobs")
def list_jobs() -> dict[str, Any]:
    # Results are omitted here on purpose: a listing that inlined every parsed
    # document would be megabytes and would blow the proxy's body cap.
    return {"jobs": [job.snapshot(include_result=False) for job in STORE.list()]}


@app.get("/jobs/{job_id}")
def get_job(job_id: str) -> JSONResponse:
    job = STORE.get(job_id)
    if job is None:
        return JSONResponse({"error": "unknown job"}, status_code=404)
    return JSONResponse(job.snapshot())


@app.delete("/jobs/{job_id}")
def cancel_job(job_id: str) -> JSONResponse:
    job = STORE.cancel(job_id)
    if job is None:
        return JSONResponse({"error": "unknown job"}, status_code=404)
    return JSONResponse(job.snapshot(include_result=False))
