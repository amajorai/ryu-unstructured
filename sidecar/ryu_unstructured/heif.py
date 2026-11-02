"""Safe HEIF decoding for Unstructured's pi-heif import.

`unstructured[pdf|all-docs]` pulls in discontinued `pi-heif`, whose last release
bundles libheif 1.23.0 (CVE-2026-120208 / GHSA-wqmc-6h9q-v6wp). Unstructured
registers HEIF with Pillow via `from pi_heif import register_heif_opener`, so
HEIC/HEIF in a corpus — and HEIF tiles embedded in PDFs — hit that decoder.

We install `pillow-heif` (libheif >= 1.23.3) and alias it as `pi_heif` in
``sys.modules`` *before* Unstructured's PDF partitioner imports, so the fixed
library is the only opener registered. When the safe backend is absent, parsing
refuses only if a HEIF decoder could still run: a transitive `pi-heif` is
importable, or the document itself is a HEIF-family image. Narrow installs such
as the `office` extra carry no HEIF decoder at all and parse normally.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from typing import Final

MIN_SAFE_LIBHEIF: Final[tuple[int, ...]] = (1, 23, 3)

# Extensions whose partition path always decodes HEIF-family containers.
HEIF_FAMILY_EXTENSIONS: Final[frozenset[str]] = frozenset({".heic", ".heif", ".avif"})

_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def parse_libheif_version(version: str) -> tuple[int, ...] | None:
    """Parse a libheif version string into a comparable tuple."""
    match = _VERSION_RE.search(version.strip())
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())


def bundled_libheif_version() -> str | None:
    """Return the libheif version bundled with ``pillow-heif``, if importable."""
    try:
        import pillow_heif
    except ImportError:
        return None
    try:
        return str(pillow_heif.libheif_version())
    except Exception:
        return None


def libheif_is_safe(version: str | None) -> bool:
    parsed = parse_libheif_version(version or "")
    return parsed is not None and parsed >= MIN_SAFE_LIBHEIF


def install_pi_heif_shim() -> str:
    """Point ``pi_heif`` at ``pillow_heif`` and return the bundled libheif version."""
    try:
        import pillow_heif
    except ImportError as exc:
        raise RuntimeError(
            "pillow-heif is not installed — install the sidecar with "
            '`pip install -e ".[all-docs]"` (or `.[pdf]`) so HEIF decoding uses '
            "libheif >= 1.23.3 instead of unstructured's vulnerable pi-heif pin."
        ) from exc

    version = bundled_libheif_version()
    if not libheif_is_safe(version):
        raise RuntimeError(
            "pillow-heif is present but its bundled libheif is too old "
            f"({version!r}); need >= 1.23.3 for CVE-2026-120208."
        )

    sys.modules["pi_heif"] = pillow_heif
    return version or ""


def _importable(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def ensure_safe_heif_decoder(path: Path | None = None) -> None:
    """Install the shim or raise ``RuntimeError`` when HEIF decoding would be unsafe.

    Without ``pillow-heif`` this is a no-op unless a HEIF decoder could still
    run: the vulnerable ``pi_heif`` is importable, or ``path`` is a HEIF-family
    image that cannot be decoded safely.
    """
    if _importable("pillow_heif"):
        install_pi_heif_shim()
        return
    if _importable("pi_heif"):
        install_pi_heif_shim()
    if path is not None and path.suffix.lower() in HEIF_FAMILY_EXTENSIONS:
        install_pi_heif_shim()


def heif_dependency_message() -> str:
    return (
        "safe HEIF decoding requires `pillow-heif>=1.7.0` (libheif >= 1.23.3). "
        "Unstructured's transitive `pi-heif` is discontinued and bundles a "
        "vulnerable libheif; reinstall the sidecar extra so pillow-heif is present."
    )
