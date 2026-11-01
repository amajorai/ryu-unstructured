"""System-dependency detection.

This is the whole reason Unstructured is the fiddliest of the four parse
backends: `pip install "unstructured[all-docs]"` gets you the Python side and
*none* of the native binaries it shells out to. A missing `soffice` surfaces
deep inside a converter as a `FileNotFoundError` or an empty element list, which
would reach the user as "this document has no text" — a plausible-looking lie.

So we probe before parsing and turn a missing binary into a job error that names
the package and the install command.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass


@dataclass(frozen=True)
class SystemDep:
    """One native dependency: how to detect it and how to install it."""

    key: str
    # Any one of these on PATH satisfies the dependency (libreoffice ships as
    # `soffice` on macOS and as `libreoffice` on most Linux distros).
    binaries: tuple[str, ...]
    purpose: str
    brew: str
    apt: str

    def present(self) -> bool:
        return any(shutil.which(binary) for binary in self.binaries)

    def describe(self) -> dict[str, object]:
        return {
            "key": self.key,
            "present": self.present(),
            "purpose": self.purpose,
            "install": {"brew": self.brew, "apt": self.apt},
        }

    def message(self) -> str:
        return (
            f"{self.key} is not installed — {self.purpose}. "
            f"Install it with `{self.brew}` (macOS) or `{self.apt}` (Debian/Ubuntu)."
        )


POPPLER = SystemDep(
    key="poppler",
    binaries=("pdfinfo", "pdftoppm"),
    purpose="PDF page rendering, needed by the hi_res and OCR strategies",
    brew="brew install poppler",
    apt="apt-get install -y poppler-utils",
)
TESSERACT = SystemDep(
    key="tesseract",
    binaries=("tesseract",),
    purpose="OCR for scanned PDFs and images",
    brew="brew install tesseract",
    apt="apt-get install -y tesseract-ocr",
)
LIBREOFFICE = SystemDep(
    key="libreoffice",
    binaries=("soffice", "libreoffice"),
    purpose="converting legacy binary Office formats (.doc/.ppt/.xls) before parsing",
    brew="brew install --cask libreoffice",
    apt="apt-get install -y libreoffice",
)
PANDOC = SystemDep(
    key="pandoc",
    binaries=("pandoc",),
    purpose="converting .epub/.rtf/.odt/.org/.rst documents before parsing",
    brew="brew install pandoc",
    apt="apt-get install -y pandoc",
)

ALL_DEPS: tuple[SystemDep, ...] = (POPPLER, TESSERACT, LIBREOFFICE, PANDOC)

# Extension → dependencies that must be present for that format to parse at all.
# Deliberately conservative: `.pdf` is listed against poppler/tesseract only in
# `OPTIONAL_BY_EXT` because a born-digital PDF parses fine on the `fast` strategy
# with neither binary, and hard-failing it would be wrong.
REQUIRED_BY_EXT: dict[str, tuple[SystemDep, ...]] = {
    ".doc": (LIBREOFFICE,),
    ".ppt": (LIBREOFFICE,),
    ".xls": (LIBREOFFICE,),
    ".epub": (PANDOC,),
    ".rtf": (PANDOC,),
    ".odt": (PANDOC,),
    ".org": (PANDOC,),
    ".rst": (PANDOC,),
    ".png": (TESSERACT,),
    ".jpg": (TESSERACT,),
    ".jpeg": (TESSERACT,),
    ".tiff": (TESSERACT,),
    ".tif": (TESSERACT,),
    ".bmp": (TESSERACT,),
    ".heic": (TESSERACT,),
}

# Extension → dependencies that unlock *better* output but whose absence still
# leaves a usable result. Reported as warnings on the job, never as an error.
OPTIONAL_BY_EXT: dict[str, tuple[SystemDep, ...]] = {
    ".pdf": (POPPLER, TESSERACT),
}


def missing_required(ext: str) -> list[SystemDep]:
    """Dependencies whose absence makes this extension unparseable."""
    return [dep for dep in REQUIRED_BY_EXT.get(ext.lower(), ()) if not dep.present()]


def missing_optional(ext: str) -> list[SystemDep]:
    """Dependencies whose absence degrades but does not break this extension."""
    return [dep for dep in OPTIONAL_BY_EXT.get(ext.lower(), ()) if not dep.present()]


def libmagic_available() -> bool:
    """Whether `python-magic`'s libmagic binding imports.

    Unstructured uses it for content-type sniffing when the filename is
    uninformative. `import magic` raises ImportError when the *native* libmagic
    is missing, not just when the wheel is, which is exactly the case we want to
    report.
    """
    try:
        import magic  # noqa: F401
    except Exception:
        return False
    return True


def unstructured_version() -> str | None:
    """Installed `unstructured` version, or None when the library is absent."""
    try:
        from unstructured.__version__ import __version__ as version
    except Exception:
        try:
            from importlib.metadata import version as pkg_version

            return pkg_version("unstructured")
        except Exception:
            return None
    return version


def snapshot() -> dict[str, object]:
    """Everything a caller needs to explain why a parse will or will not work."""
    version = unstructured_version()
    deps = [dep.describe() for dep in ALL_DEPS]
    return {
        "backend": "unstructured",
        "library_available": version is not None,
        "library_version": version,
        "libmagic": libmagic_available(),
        "system_dependencies": deps,
        "missing_system_dependencies": [
            dep["key"] for dep in deps if not dep["present"]
        ],
    }
