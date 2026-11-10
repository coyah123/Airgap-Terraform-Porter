"""terraform / gpg discovery and the per-OS bits (scrolling, cache folder).

    python -m unittest discover -s tests
"""
from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import plan_ops  # noqa: E402
import tf_locate  # noqa: E402


def fake_terraform(folder: Path, version: str) -> Path:
    """A stand-in `terraform` that answers `terraform version -json`."""
    out = '{"terraform_version":"%s"}' % version
    if sys.platform == "win32":
        p = folder / "terraform.bat"
        p.write_text(f"@echo off\r\necho {out}\r\n")
    else:
        p = folder / "terraform"
        p.write_text(f"#!/bin/sh\necho '{out}'\n")
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return p


class TerraformOnPath(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        tf_locate.terraform_version.cache_clear()

    def tearDown(self):
        self.tmp.cleanup()
        tf_locate.terraform_version.cache_clear()

    def test_launching_terminal_path_wins(self):
        fake = fake_terraform(self.dir, "9.9.9")
        path = str(self.dir) + os.pathsep + os.environ.get("PATH", "")
        with mock.patch.dict(os.environ, {"PATH": path}):
            first = tf_locate.find_all()[0]
            best = tf_locate.default_terraform()
        self.assertEqual(Path(first.path).resolve(), fake.resolve())
        self.assertEqual((first.source, first.version), ("PATH", "9.9.9"))
        self.assertEqual(best, first)
        self.assertIn("from PATH", first.label)

    def test_bundle_used_only_when_nothing_else(self):
        bundle = self.dir / "bundle"
        host = "windows_amd64" if sys.platform == "win32" else "linux_amd64"
        b = bundle / "bin" / host
        b.mkdir(parents=True)
        real = fake_terraform(b, "1.17.1")
        exe = b / tf_locate.EXE
        real.rename(exe) if real != exe else None
        with mock.patch.dict(os.environ, {"PATH": ""}), \
                mock.patch.object(tf_locate, "login_shell_path", lambda: ""), \
                mock.patch.object(tf_locate, "_common_dirs", lambda name: []):
            cands = tf_locate.find_all(bundle, host)
        self.assertEqual([c.source for c in cands], ["bundle"])

    def test_login_shell_used_when_path_is_minimal(self):
        fake = fake_terraform(self.dir, "1.15.0")
        with mock.patch.dict(os.environ, {"PATH": ""}), \
                mock.patch.object(tf_locate, "login_shell_path", lambda: str(self.dir)), \
                mock.patch.object(tf_locate, "_common_dirs", lambda name: []):
            c = tf_locate.find_all()[0]
        self.assertEqual((Path(c.path).resolve(), c.source), (fake.resolve(), "shell"))

    def test_unrunnable_binary_has_no_version(self):
        junk = self.dir / "terraform.exe"
        junk.write_bytes(b"not a program")
        self.assertEqual(tf_locate.describe(str(junk)).version, "")


class BundleDetection(unittest.TestCase):
    def test_found_by_contents_not_name(self):
        import plan_view
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "repo"
            stack = repo / "02-stack"
            stack.mkdir(parents=True)
            (repo / "carry-me").mkdir()
            (repo / "carry-me" / "providers").mkdir()
            self.assertIsNone(plan_view.find_bundle(stack))        # no MANIFEST yet
            (repo / "carry-me" / "MANIFEST.json").write_text("{}")
            self.assertEqual(plan_view.find_bundle(stack), repo / "carry-me")


class PerOS(unittest.TestCase):
    def test_wheel_units(self):
        import plan_view
        self.assertEqual(plan_view.wheel_units(120, "win32"), -1)
        self.assertEqual(plan_view.wheel_units(-240, "win32"), 2)
        self.assertEqual(plan_view.wheel_units(30, "win32"), -1)   # small deltas still move
        self.assertEqual(plan_view.wheel_units(3, "darwin"), -3)   # macOS / trackpad
        self.assertEqual(plan_view.wheel_units(-1, "darwin"), 1)

    def test_cache_root(self):
        with mock.patch.object(plan_ops.sys, "platform", "darwin"):
            self.assertEqual(plan_ops.cache_root(),
                             Path.home() / "Library" / "Caches" / "airgap-terraform-porter")
        with mock.patch.object(plan_ops.sys, "platform", "linux"), \
                mock.patch.dict(os.environ, {"XDG_CACHE_HOME": "/tmp/xdg"}):
            self.assertEqual(plan_ops.cache_root(), Path("/tmp/xdg/airgap-terraform-porter"))

    def test_host_platform_names(self):
        import bundle_ops
        with mock.patch.object(bundle_ops.sys, "platform", "darwin"), \
                mock.patch("platform.machine", return_value="arm64"):
            self.assertEqual(bundle_ops.host_platform(), "darwin_arm64")
        with mock.patch.object(bundle_ops.sys, "platform", "linux"), \
                mock.patch("platform.machine", return_value="x86_64"):
            self.assertEqual(bundle_ops.host_platform(), "linux_amd64")


if __name__ == "__main__":
    unittest.main()
