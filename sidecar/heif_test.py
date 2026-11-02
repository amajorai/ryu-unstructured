"""Regression tests for the libheif / pi-heif security shim."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from ryu_unstructured.heif import (
    MIN_SAFE_LIBHEIF,
    ensure_safe_heif_decoder,
    install_pi_heif_shim,
    libheif_is_safe,
    parse_libheif_version,
)


class LibheifVersionTest(unittest.TestCase):
    def test_parse_libheif_version(self) -> None:
        self.assertEqual(parse_libheif_version("1.23.6"), (1, 23, 6))
        self.assertIsNone(parse_libheif_version("not-a-version"))

    def test_libheif_is_safe_threshold(self) -> None:
        self.assertTrue(libheif_is_safe("1.23.3"))
        self.assertTrue(libheif_is_safe("1.23.6"))
        self.assertFalse(libheif_is_safe("1.23.2"))
        self.assertFalse(libheif_is_safe(None))


class PiHeifShimTest(unittest.TestCase):
    def test_install_pi_heif_shim_aliases_pillow_heif(self) -> None:
        try:
            import pillow_heif
        except ImportError:
            self.skipTest("pillow-heif not installed")

        version = install_pi_heif_shim()
        self.assertGreaterEqual(parse_libheif_version(version) or (), MIN_SAFE_LIBHEIF)
        self.assertIs(sys.modules["pi_heif"], pillow_heif)

    def test_ensure_safe_heif_decoder_requires_pillow_heif(self) -> None:
        with patch.dict(sys.modules, {"pillow_heif": None}):
            with patch(
                "ryu_unstructured.heif.install_pi_heif_shim",
                side_effect=RuntimeError("missing"),
            ):
                with self.assertRaises(RuntimeError):
                    ensure_safe_heif_decoder(Path("scan.heif"))


class HeifGateTest(unittest.TestCase):
    """Without pillow-heif, only refuse when a HEIF decoder could still run."""

    def setUp(self) -> None:
        hidden = patch.dict(sys.modules, {"pillow_heif": None})
        hidden.start()
        self.addCleanup(hidden.stop)

    def _installed(self, *modules: str):
        return patch(
            "ryu_unstructured.heif._importable",
            side_effect=lambda module: module in modules,
        )

    def test_office_only_install_parses_documents(self) -> None:
        with self._installed():
            ensure_safe_heif_decoder(Path("report.docx"))
            ensure_safe_heif_decoder(Path("deck.pptx"))
            ensure_safe_heif_decoder(Path("sheet.xlsx"))

    def test_office_only_install_refuses_heif_images(self) -> None:
        with self._installed():
            with self.assertRaises(RuntimeError):
                ensure_safe_heif_decoder(Path("photo.HEIC"))

    def test_vulnerable_pi_heif_without_pillow_heif_is_refused(self) -> None:
        with self._installed("pi_heif"):
            with self.assertRaises(RuntimeError):
                ensure_safe_heif_decoder(Path("report.pdf"))


if __name__ == "__main__":
    unittest.main()
