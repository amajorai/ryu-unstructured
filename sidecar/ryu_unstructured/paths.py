"""Path confinement and safe archive expansion.

Two separate jobs, both fail-closed:

1. `resolve_input` — the parse request names a file by *path* (Core hands over a
   content-addressed blob under `~/.ryu/blobs/…`, never an upload), so the path
   is attacker-influenced input. It is resolved through symlinks and then
   required to live under an allow-listed root. Without the post-resolution
   containment check, a symlink planted inside the blob dir reads `/etc/shadow`
   and returns it as "document text".

2. `safe_extract` — an archive's member names are attacker-controlled strings.
   Absolute names, `..` segments, and symlink/hardlink/device members are all
   rejected outright rather than sanitised, because a rewritten name is a guess
   at intent and a refusal is not.

`safe_basename` and `named_view` serve the third: the *display* name. Core hands
over `path` (a content-addressed blob with no extension) and `filename` (the
document's real name) as two separate fields, so the name is caller-supplied
string data that reaches both the format dispatch and the desktop chip.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import tarfile
import tempfile
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Optional

from .limits import MAX_ARCHIVE_BYTES, MAX_ARCHIVE_MEMBERS, MAX_INPUT_BYTES

# A filename is echoed back on every poll and rendered as the attachment chip, so
# it is bounded like any other untrusted string that reaches a UI.
MAX_DISPLAY_NAME_CHARS = 255


class InputError(ValueError):
    """A caller-supplied path or archive that we refuse to open."""


def allowed_roots() -> list[Path]:
    """Directories a parse input may live under.

    `RYU_UNSTRUCTURED_ROOTS` is a `os.pathsep`-separated list Core sets from the
    manifest (`${RYU_DIR}` is the only token the manifest may interpolate). With
    nothing set we fall back to the Core data dir so a bare `python -m` run is
    still confined — an empty allow-list must never mean "everything".
    """
    raw = (os.environ.get("RYU_UNSTRUCTURED_ROOTS") or "").strip()
    if not raw:
        raw = os.environ.get("RYU_DIR") or str(Path.home() / ".ryu")
    roots: list[Path] = []
    for part in raw.split(os.pathsep):
        candidate = part.strip()
        if not candidate:
            continue
        try:
            roots.append(Path(candidate).expanduser().resolve())
        except OSError:
            continue
    return roots


def _is_within(child: Path, parent: Path) -> bool:
    """True when `child` is `parent` or sits beneath it.

    `Path.is_relative_to` is 3.9+, and both sides are already fully resolved, so
    this is a pure lexical comparison over real paths.
    """
    return child == parent or parent in child.parents


def resolve_input(raw_path: str) -> Path:
    """Resolve a requested input path, or raise `InputError`.

    Symlinks are followed *before* the containment test on purpose: the question
    is where the bytes actually live, not what the name looks like.
    """
    candidate = (raw_path or "").strip()
    if not candidate:
        raise InputError("missing `path`")
    try:
        resolved = Path(candidate).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise InputError(f"cannot open `{candidate}`: {exc}") from exc
    if not resolved.is_file():
        raise InputError(f"`{candidate}` is not a regular file")

    roots = allowed_roots()
    if not any(_is_within(resolved, root) for root in roots):
        readable = ", ".join(str(root) for root in roots) or "(none)"
        raise InputError(
            f"`{candidate}` resolves outside the allowed roots ({readable}); "
            "set RYU_UNSTRUCTURED_ROOTS to widen them"
        )

    size = resolved.stat().st_size
    if size > MAX_INPUT_BYTES:
        raise InputError(
            f"input is {size} bytes, over the {MAX_INPUT_BYTES}-byte limit "
            "(raise RYU_UNSTRUCTURED_MAX_INPUT_BYTES to allow it)"
        )
    return resolved


def safe_basename(raw: Optional[str]) -> Optional[str]:
    """The display name of a caller-supplied `filename`, or None.

    Only the *last* segment survives, on both separators, so a `filename` of
    `../../etc/passwd` becomes `passwd` rather than steering a write or being
    echoed into the desktop's attachment chip as a path. Control characters go
    the same way: this string is rendered, and it is the only part of the request
    that a document's author gets to choose.
    """
    if raw is None:
        return None
    candidate = str(raw).replace("\\", "/").rsplit("/", 1)[-1].strip()
    candidate = "".join(ch for ch in candidate if ch.isprintable()).strip()
    # `.` and `..` are the only all-dot names, and neither is a document. A
    # leading dot otherwise survives — `.gitignore` is a real file.
    if not candidate or not candidate.strip("."):
        return None
    if len(candidate) <= MAX_DISPLAY_NAME_CHARS:
        return candidate
    # Clip the *stem*, never the extension: a 300-character filename that lost
    # its `.docx` would fall back to content sniffing, which is the failure this
    # whole display-name path exists to prevent.
    suffix = Path(candidate).suffix[: MAX_DISPLAY_NAME_CHARS // 2]
    return candidate[: MAX_DISPLAY_NAME_CHARS - len(suffix)] + suffix


def extension_of(name: str) -> str:
    """The lowercase final extension of a *name*, dot included, or `""`.

    Taken from the display name rather than the on-disk path: the primary submit
    form points at `blobs/<shard>/<sha256>`, which has no extension at all, so
    dispatching on the path would silently skip every format check.
    """
    return Path((name or "").replace("\\", "/")).suffix.lower()


ARCHIVE_SUFFIXES = (".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")


def is_archive_name(name: str) -> bool:
    """Whether a *name* denotes an archive we expand and parse member-by-member.

    Matches on the whole name rather than `Path.suffix`, because the suffix of
    `report.tar.gz` is `.gz` and expanding it as a gzip stream is not what the
    caller asked for.
    """
    lowered = (name or "").lower()
    return any(lowered.endswith(suffix) for suffix in ARCHIVE_SUFFIXES)


def sweep_dispatch_links() -> None:
    """Drop dispatch-link directories left behind by a previous run.

    `named_view` cleans up in a `finally`, which covers an exception but not a
    kill — and this sidecar is `lazy` + `idle_stop_secs`, so it genuinely can be
    reaped mid-parse. Each leaked link is a *hard* link, so it pins the blob's
    inode: a document deleted from a Space would keep its bytes on disk forever
    behind a link nobody can see. Called once at startup, before serving.
    """
    names = Path(os.environ.get("RYU_UNSTRUCTURED_WORKDIR") or tempfile.gettempdir())
    names = names.expanduser() / "names"
    if not names.is_dir():
        return
    for stale in names.iterdir():
        if stale.name.startswith("name-"):
            shutil.rmtree(stale, ignore_errors=True)


def workdir() -> Path:
    """Scratch directory this sidecar owns. Core points it inside `${RYU_DIR}`."""
    configured = (os.environ.get("RYU_UNSTRUCTURED_WORKDIR") or "").strip()
    root = Path(configured).expanduser() if configured else Path(tempfile.gettempdir())
    root.mkdir(parents=True, exist_ok=True)
    return root


@contextlib.contextmanager
def named_view(path: Path, name: str) -> Iterator[Path]:
    """Yield a path whose extension matches `name`, for the format dispatch.

    An extensionless blob is genuinely ambiguous to a content sniffer — `.docx`,
    `.pptx`, `.xlsx` and `.epub` are all ZIP containers, and a `.txt` has no
    magic bytes at all — so the document's real extension has to reach the
    partitioner somehow.

    It is a **hard** link, and that is not interchangeable with a symlink:
    Unstructured resolves a symlinked path with `os.path.realpath` before it
    reads the extension off it (`file_utils/filetype.py`), so a symlink named
    `input.docx` is silently undone. A hard link is not a link as far as
    `os.path.islink` is concerned, so the name survives. The link is made inside
    our own workdir, which Core puts under the same `${RYU_DIR}` as the blob
    store and therefore the same filesystem.

    `partition(file=…, metadata_filename=…)` would be the other way, but its
    extension only wins when the file object has no `.name` — i.e. after reading
    the whole document into memory, which is exactly what the `path` form exists
    to avoid at 200 MiB.

    When no link can be made (a cross-device root, a filesystem without hard
    links) the original path is handed back unchanged and the caller falls back
    to asserting a content type. Degraded dispatch, never a failed parse.
    """
    desired = extension_of(name)
    if not desired or path.suffix.lower() == desired:
        yield path
        return
    try:
        names = workdir() / "names"
        names.mkdir(parents=True, exist_ok=True)
        scratch = Path(tempfile.mkdtemp(prefix="name-", dir=str(names)))
    except OSError:
        yield path
        return
    try:
        link = scratch / f"input{desired}"
        try:
            os.link(path, link)
        except (OSError, NotImplementedError, AttributeError):
            yield path
            return
        yield link
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _check_member_name(name: str) -> str:
    """Reject a member name that escapes, or return its normalised relative form."""
    if not name or name in (".", "./"):
        raise InputError("archive member with an empty name")
    if name.startswith("/") or name.startswith("\\"):
        raise InputError(f"archive member `{name}` is an absolute path")
    pure = Path(name.replace("\\", "/"))
    if pure.is_absolute() or pure.drive or pure.root:
        raise InputError(f"archive member `{name}` is an absolute path")
    if any(part == ".." for part in pure.parts):
        raise InputError(f"archive member `{name}` contains a parent-directory reference")
    return str(pure)


def _finalise(dest_root: Path, relative: str) -> Path:
    """Belt-and-braces containment check on the concrete destination path."""
    target = (dest_root / relative).resolve()
    if not _is_within(target, dest_root):
        raise InputError(f"archive member `{relative}` escapes the extraction directory")
    return target


def safe_extract(archive: Path, dest_root: Path) -> list[Path]:
    """Expand `archive` into `dest_root`, returning the extracted regular files.

    Directories are created as needed; every other member kind (symlink,
    hardlink, fifo, device) is refused, since none of them can carry document
    bytes and all of them can redirect a later write.
    """
    dest_root = dest_root.resolve()
    dest_root.mkdir(parents=True, exist_ok=True)
    if zipfile.is_zipfile(archive):
        return _extract_zip(archive, dest_root)
    try:
        if tarfile.is_tarfile(archive):
            return _extract_tar(archive, dest_root)
    except (OSError, tarfile.TarError) as exc:
        raise InputError(f"unreadable archive: {exc}") from exc
    raise InputError(f"`{archive.name}` is not a readable zip or tar archive")


def _extract_zip(archive: Path, dest_root: Path) -> list[Path]:
    written: list[Path] = []
    total = 0
    with zipfile.ZipFile(archive) as zf:
        infos = zf.infolist()
        if len(infos) > MAX_ARCHIVE_MEMBERS:
            raise InputError(
                f"archive has {len(infos)} members, over the {MAX_ARCHIVE_MEMBERS} limit"
            )
        for info in infos:
            relative = _check_member_name(info.filename)
            target = _finalise(dest_root, relative)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            # The high 16 bits of external_attr are the unix mode; S_IFLNK there
            # is a symlink member, which `ZipFile.extract` would write as a file
            # containing the link target and some tools would then follow.
            mode = info.external_attr >> 16
            if mode and (mode & 0o170000) == 0o120000:
                raise InputError(f"archive member `{info.filename}` is a symlink")
            total += info.file_size
            if total > MAX_ARCHIVE_BYTES:
                raise InputError(
                    f"archive expands past the {MAX_ARCHIVE_BYTES}-byte limit"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, target.open("wb") as dst:
                _copy_bounded(src, dst, MAX_ARCHIVE_BYTES)
            written.append(target)
    return written


def _extract_tar(archive: Path, dest_root: Path) -> list[Path]:
    written: list[Path] = []
    total = 0
    with tarfile.open(archive) as tf:
        count = 0
        for member in tf:
            count += 1
            if count > MAX_ARCHIVE_MEMBERS:
                raise InputError(
                    f"archive has over {MAX_ARCHIVE_MEMBERS} members"
                )
            relative = _check_member_name(member.name)
            target = _finalise(dest_root, relative)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if member.issym() or member.islnk():
                raise InputError(f"archive member `{member.name}` is a link")
            if not member.isfile():
                raise InputError(f"archive member `{member.name}` is not a regular file")
            total += member.size
            if total > MAX_ARCHIVE_BYTES:
                raise InputError(
                    f"archive expands past the {MAX_ARCHIVE_BYTES}-byte limit"
                )
            src = tf.extractfile(member)
            if src is None:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with src, target.open("wb") as dst:
                _copy_bounded(src, dst, MAX_ARCHIVE_BYTES)
            written.append(target)
    return written


def _copy_bounded(src, dst, ceiling: int) -> None:
    """Stream member bytes, stopping if the declared size was a lie."""
    remaining = ceiling
    while True:
        chunk = src.read(64 * 1024)
        if not chunk:
            return
        remaining -= len(chunk)
        if remaining < 0:
            raise InputError("archive member is larger than its declared size")
        dst.write(chunk)
