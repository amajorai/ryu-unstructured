# ryu-unstructured

Unstructured for Ryu — document parsing via the Apache-2.0 Unstructured library: the broadest-coverage `document.parse` backend, reading roughly sixty formats including the whole Office family.

> **The public home of `ryu-unstructured`.** Source, builds, and releases live here —
> binaries for every platform are attached to each release.
>
> This tree is generated from the Ryu monorepo, so commits pushed here
> directly are replaced on the next sync. **Pull requests are welcome** —
> open them here and they are ported into the monorepo, then flow back out.
> Ryu as a whole: https://github.com/amajorai/ryu

## Source & build

The **source of record** for the universal Ryu TTS sidecar — a self-contained
Python HTTP front over several text-to-speech engines. Install its
dependencies (`pip install -r sidecar/requirements.txt`) and run
`python -m ryu_tts` from `sidecar/`; Core manages it as a sidecar in a
full Ryu install.

## License

Apache-2.0 — see [LICENSE](./LICENSE).

---

# Unstructured — `document.parse` backend

Turns documents into markdown using the Apache-2.0
[`unstructured`](https://github.com/Unstructured-IO/unstructured) library. This is
one of several interchangeable backends behind the swappable `document.parse`
capability: enable it, pick it in the provider selector, and everything that
ingests a document (Spaces, RAG, chat attachments) routes through it. Nothing in
Core is bound to it — the swap is manifest data.

## What it is good at

**Format breadth.** Roughly sixty extensions, and it is the only one of the four
backends that reads the *legacy binary* Office formats (`.doc`, `.ppt`, `.xls`)
and `.msg`/`.eml` email. If your corpus is a shared drive that accumulated for
fifteen years, this is the backend that opens it.

**Element-level output.** `partition()` returns typed fragments — `Title`,
`NarrativeText`, `ListItem`, `Table`, `CodeSnippet`, `PageBreak`, `FigureCaption`
— not a character soup. This sidecar renders those structurally rather than
string-joining them:

| Element | Markdown |
| --- | --- |
| `Title` (with `category_depth`) | `#` … `######` at the detected depth |
| `ListItem` | `- item`, consecutive items kept adjacent |
| `Table` | a real markdown table rebuilt from `metadata.text_as_html` |
| `CodeSnippet` | fenced block |
| `Formula` | `$$ … $$` |
| `FigureCaption` / `Image` | italic caption |
| `PageNumber` / `Header` / `Footer` | dropped (chunk noise, matches nothing) |

The raw records are also returned under `elements`, so a consumer that wants to
chunk on structure rather than on character count can.

Archives (`.zip`, `.tar[.gz|.bz2|.xz]`) are expanded and parsed member by member,
with each member nested under its own filename heading. One unreadable member
becomes a warning, not a failed archive.

## What it costs

**Install weight is the honest downside.** `pip install "unstructured[all-docs]"`
pulls ONNX Runtime, layout-detection models, NLTK data and (on some platforms)
torch — budget **1–2 GB** and several minutes. The narrower extras defined in
`sidecar/pyproject.toml` are much cheaper when you know your corpus:

```
pip install -e ".[office]"   # docx/pptx/xlsx only
pip install -e ".[pdf]"      # pdf only
pip install -e ".[all-docs]" # everything (what the manifest installs)
```

**Native tools are not pip-installable.** This is the main failure mode, and the
one this sidecar works hardest to make legible. `unstructured` shells out to:

| Tool | Needed for | macOS | Debian/Ubuntu |
| --- | --- | --- | --- |
| poppler | PDF page rendering (`hi_res` / OCR strategies) | `brew install poppler` | `apt-get install -y poppler-utils` |
| tesseract | OCR of scanned PDFs and images | `brew install tesseract` | `apt-get install -y tesseract-ocr` |
| libreoffice | `.doc` / `.ppt` / `.xls` conversion | `brew install --cask libreoffice` | `apt-get install -y libreoffice` |
| pandoc | `.epub` / `.rtf` / `.odt` / `.org` / `.rst` | `brew install pandoc` | `apt-get install -y pandoc` |
| libmagic | content-type sniffing when the filename is uninformative | `brew install libmagic` | `apt-get install -y libmagic1` |

A missing tool normally surfaces deep inside a converter as a `FileNotFoundError`
or, worse, an empty element list — which reaches the user as "this document has
no text", a plausible-looking lie. So the sidecar **probes before parsing**: a
format whose required tool is absent fails the job with
`error_code: "missing_dependency"`, a `missing_dependencies: ["libreoffice"]`
list, and a message naming the install command. `GET /capability` reports the
same picture up front, and answers even when the library itself is not installed
yet — which is exactly when you are deciding whether to install it.

Tools that only *improve* a result (poppler and tesseract on a born-digital PDF,
which parses fine on the `fast` strategy without either) are reported as job
`warnings`, never as failures.

**Hardware.** CPU-only and no GPU is required. The `fast` strategy is I/O-bound
and cheap. The `hi_res` strategy runs a layout-detection model per page — expect
roughly 1–3 s/page on a modern laptop CPU and a few hundred MB of RSS while a
model is loaded, so plan on **~4 GB RAM** for comfortable `hi_res` work. OCR over
a large scanned PDF is the slow case and is why the default per-parse timeout is
600 s. Two parses run concurrently by default (`RYU_UNSTRUCTURED_MAX_WORKERS`).

## Choosing between backends

Pick this one when coverage matters more than speed, when the corpus contains
legacy Office or email formats, or when you want element categories to survive
into chunking. Pick a lighter backend when the corpus is modern PDFs and DOCX and
you would rather not carry a 1–2 GB install and four `brew`/`apt` packages.

## HTTP contract

Reachable at `/api/ext/@ryu/unstructured/*`. Every path below is declared in
`manifest.json`; an undeclared path is refused with a 404 at the proxy before it
reaches this process.

```
GET    /health          -> { ok, backend, available, library_version, missing_dependencies }
GET    /capability      -> { capability, backend, formats, system_dependencies, limits }
POST   /parse           -> 202 { job_id, status }
GET    /jobs            -> { jobs: [ snapshot without result ] }
GET    /jobs/{job_id}   -> snapshot (result present once succeeded)
DELETE /jobs/{job_id}   -> snapshot (cooperative cancel)
```

`POST /parse` takes exactly one of `path` (absolute, confined to
`RYU_UNSTRUCTURED_ROOTS`) or `content_base64`, plus optional `options`:
`strategy` (`auto` | `fast` | `hi_res` | `ocr_only`), `languages`,
`infer_table_structure`. Unknown option keys are ignored rather than rejected —
a hint one backend understands must not fail on another. `blob_sha256`, `mime`
and `size_bytes` are accepted and advisory; the file is re-`stat`ed and the
extension wins over the MIME type.

### `filename` is load-bearing on **both** forms

Core's primary submit form points `path` at a content-addressed blob —
`~/.ryu/blobs/<shard>/<sha256>`, **no extension** — and carries the document's
real name in a separate `filename` field. So every format decision here keys off
`filename`, never the path:

- the job's own `filename`, which is what the attachment chip renders (a poll
  answering 64 hex characters is a bug, not a detail);
- the native-tool preflight — `legacy.doc` must fail with "libreoffice is not
  installed", and an extensionless path would consult nothing at all;
- archive detection, so a `.zip` still goes through `safe_extract` rather than
  straight into `partition()`;
- the partitioner dispatch. Unstructured resolves a **sym**link with
  `os.path.realpath` before reading the extension off it, so the blob is given a
  **hard** link named `input.<ext>` inside `RYU_UNSTRUCTURED_WORKDIR` for the
  duration of the parse (no copy, and the workdir shares `${RYU_DIR}`'s
  filesystem with the blob store). Where a hard link cannot be made, the
  extension's content type is asserted to `partition` instead.

Only the basename of `filename` is ever used, and only its extension steers a
write — a `filename` of `../../etc/passwd` becomes `passwd`.

A succeeded job's `result` is:

```jsonc
{
  "backend": "unstructured",
  "backend_version": "0.24.1",
  "markdown": "# Quarterly Report\n\n…",   // primary payload
  "text": "Quarterly Report\n\n…",          // markup-free fallback
  "elements": [ { "category": "Title", "text": "…", "page_number": 1, … } ],
  "warnings": [ "tesseract is not installed — …" ],
  "truncated": false,
  "metadata": { "filename": "q3.pdf", "element_count": 412, "page_count": 18, "sources": ["q3.pdf"] }
}
```

Failed jobs carry `error`, `error_code` (`library_missing`, `missing_dependency`,
`unsupported_format`, `parse_failed`, `input_rejected`, `timeout`) and
`missing_dependencies`.

### Why submit-and-poll rather than one request

The ext-proxy's activity guard drops as soon as response headers arrive, so a
`lazy` + `idle_stop_secs` sidecar can be reaped **mid-request**. A 90-second PDF
parse behind a single HTTP call is therefore killable. Every parse is a job:
`POST /parse` answers 202 immediately and the caller polls, which re-arms the
guard on each hit.

## Security posture

- **Fail-closed bearer.** `RYU_EXT_TOKEN` is read at import time and compared with
  `hmac.compare_digest`. No token configured means *reject everything*. `/health`
  is exempt on **GET only**.
- **Path confinement.** A parse input is resolved through symlinks and then
  required to live under `RYU_UNSTRUCTURED_ROOTS` (default `${RYU_DIR}`). Without
  the post-resolution check, a symlink planted in the blob dir turns this service
  into an arbitrary-file-read primitive.
- **Archive safety.** Absolute member names, `..` segments, and
  symlink/hardlink/device members are refused outright — not sanitised. Member
  count and expanded bytes are capped, and a member larger than its declared size
  aborts the extraction.
- **Bounded everything.** Input bytes, output bytes, wall-clock per parse,
  concurrent workers, and retained jobs all have caps (see
  `sidecar/ryu_unstructured/limits.py`); each is env-overridable and reported by
  `/capability`.
- The sidecar makes **no network calls**. Unstructured's own telemetry is disabled
  via manifest env.

### Timeout honesty

CPython cannot kill a running thread, so the per-parse watchdog marks the job
`failed` at the deadline and stops waiting on it; the worker thread may run to
completion in the background and its result is discarded. From the caller's side
this is real enforcement — the job never hangs. The ceiling on wasted work is
`RYU_UNSTRUCTURED_MAX_WORKERS` stuck parses, after which new submissions queue.

## Configuration

| Env | Default | Meaning |
| --- | --- | --- |
| `RYU_UNSTRUCTURED_PORT` | 8093 | Bind port. Core injects the **profile-shifted** value (dev profile = +1000). |
| `RYU_UNSTRUCTURED_HOST` | `127.0.0.1` | Bind host. Loopback only — this process reads local files. |
| `RYU_EXT_TOKEN` | — | Shared bearer. Unset ⇒ every route except `GET /health` returns 401. |
| `RYU_UNSTRUCTURED_ROOTS` | `${RYU_DIR}` | `os.pathsep`-separated roots a parse input may live under. |
| `RYU_UNSTRUCTURED_WORKDIR` | temp dir | Scratch: inline (`content_base64`) uploads and the short-lived hard links that lend a blob its extension. Keep it on the same filesystem as the blob store. |
| `RYU_UNSTRUCTURED_MAX_INPUT_BYTES` | 200 MiB | Largest input file or archive. |
| `RYU_UNSTRUCTURED_MAX_OUTPUT_BYTES` | 8 MiB | Result cap; over it the payload is clipped and `truncated` is true. |
| `RYU_UNSTRUCTURED_TIMEOUT_SECS` | 600 | Wall-clock ceiling per parse. |
| `RYU_UNSTRUCTURED_MAX_WORKERS` | 2 | Concurrent parses. |
| `RYU_UNSTRUCTURED_MAX_JOBS` | 64 | Retained jobs before the oldest terminal ones are evicted. |
| `RYU_UNSTRUCTURED_MAX_ARCHIVE_MEMBERS` | 512 | Members expanded from one archive. |
| `RYU_UNSTRUCTURED_MAX_ARCHIVE_BYTES` | 512 MiB | Total expanded archive bytes. |

## Developing

```bash
cd sidecar
uv venv --python 3.11 .venv && source .venv/bin/activate
uv pip install -e ".[all-docs]"   # `uv venv` seeds no pip; use `uv pip`
python smoke_test.py              # contract tests; runs with or without the library
python -m ryu_unstructured        # serve on 127.0.0.1:8093
```

`smoke_test.py` covers the fail-closed bearer, the open `GET /health` (and the
closed `POST /health`), `/capability` answering without the library, a real parse
round-trip, path-confinement rejection (outside-root, `..`, escaping symlink),
archive traversal rejection, and the blob form end to end — an extensionless
path plus a `filename` must report the document's name, preflight its native
tools, reduce a traversal name to its basename, and still route an archive
through containment. It prints which mode it ran in: a run without
`unstructured` installed exercises the clean-failure path, not the parse path,
and the `libreoffice` assertion is skipped on a host that has LibreOffice.
