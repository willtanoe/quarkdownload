"""Smoke tests for the downloader CLI entry points.

These tests do not touch the network. They verify that the scripts compile,
expose a working ``--help``, and that provider detection behaves as expected.
"""
import importlib.util
import py_compile
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = {
    "downloader": ROOT / "downloader.py",
    "redguo_downloader": ROOT / "redguo_downloader.py",
}


def _has_requests() -> bool:
    return importlib.util.find_spec("requests") is not None


class CompileTests(unittest.TestCase):
    def test_scripts_compile(self):
        for path in SCRIPTS.values():
            with self.subTest(script=path.name):
                py_compile.compile(str(path), doraise=True)


@unittest.skipUnless(_has_requests(), "requests is not installed")
class CliTests(unittest.TestCase):
    def _run_help(self, name: str):
        result = subprocess.run(
            [sys.executable, str(SCRIPTS[name]), "--help"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("usage", result.stdout.lower())

    def test_downloader_help(self):
        self._run_help("downloader")

    def test_redguo_help(self):
        self._run_help("redguo_downloader")


@unittest.skipUnless(_has_requests(), "requests is not installed")
class ProviderDetectionTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("downloader", ROOT / "downloader.py")
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)

    def test_provider_detection(self):
        self.assertEqual(self.mod.detect_provider("https://pan.quark.cn/s/abc123"), "quark")
        self.assertEqual(self.mod.detect_provider("https://pan.baidu.com/s/abc?pwd=1"), "baidu")
        self.assertEqual(self.mod.detect_provider("https://example.com/s/abc"), "unknown")


if __name__ == "__main__":
    unittest.main()
