"""Offline unit tests: constraint logic, HCL-lite parsing, scanning, hashes.

    python -m unittest discover -s tests
"""
from __future__ import annotations

import base64
import hashlib
import sys
import tempfile
import textwrap
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bundle_ops  # noqa: E402
import fetch_ops  # noqa: E402
import hcl_lite  # noqa: E402
import scan_ops  # noqa: E402
from tf_versions import allows_all, newest_matching  # noqa: E402


class Versions(unittest.TestCase):
    def test_pessimistic(self):
        self.assertTrue(allows_all(["~> 5.100"], "5.100.0"))
        self.assertTrue(allows_all(["~> 5.100"], "5.101.3"))
        self.assertFalse(allows_all(["~> 5.100"], "6.0.0"))
        self.assertTrue(allows_all(["~> 5.100.1"], "5.100.9"))
        self.assertFalse(allows_all(["~> 5.100.1"], "5.101.0"))

    def test_ranges_and_prerelease(self):
        vs = ["1.9.8", "1.10.0", "1.11.0-beta1", "1.10.5", "2.0.0"]
        self.assertEqual(newest_matching(vs, [">= 1.10, < 2.0"]), "1.10.5")
        self.assertEqual(newest_matching(vs, ["1.11.0-beta1"]), "1.11.0-beta1")
        self.assertEqual(newest_matching(vs, ["!= 2.0.0"]), "1.10.5")
        self.assertIsNone(newest_matching(vs, ["> 3"]))
        self.assertEqual(newest_matching(vs, []), "2.0.0")


FIXTURE = {
    "root/versions.tf": '''
        terraform {
          required_version = ">= 1.10, < 2.0"   # comment
          required_providers {
            aws = {
              source  = "hashicorp/aws"
              version = "~> 5.100" // trailing
            }
            tls = { source = "hashicorp/tls", version = ">= 4.0" }
            time = {
              source = "hashicorp/time"
              version = "~> 0.9"
              configuration_aliases = [time.alt]
            }
          }
          backend "s3" {}
        }
        /* block comment { with braces } */
        locals {
          tpl = "${jsonencode({ a = "}" })}"
          doc = <<-EOT
            not a block {
          EOT
          m = { for k, v in var.x : k => v }
        }
        module "eks" {
          source = "./modules/eks"
        }
        module "remote" {
          source  = "terraform-aws-modules/vpc/aws"
          version = "5.0.0"
        }
        resource "random_id" "x" { byte_length = 4 }
        resource "terraform_data" "y" {}
    ''',
    "root/.terraform.lock.hcl": '''
        provider "registry.terraform.io/hashicorp/aws" {
          version     = "5.100.0"
          constraints = "~> 5.100"
          hashes = [
            "h1:abc=",
            "zh:0123",
          ]
        }
    ''',
    "root/modules/eks/main.tf": '''
        terraform {
          required_providers {
            aws       = { source = "hashicorp/aws", version = ">= 5.95, < 6.0" }
            cloudinit = { source = "hashicorp/cloudinit", version = ">= 2.0" }
            null      = { source = "hashicorp/null", version = ">= 3.0" }
          }
        }
    ''',
    "root/extra.tf.json": '''
        {"terraform": {"required_providers": {"kubernetes": {"source": "hashicorp/kubernetes", "version": "~> 2.30"}}}}
    ''',
    "root/.terraform/providers/ignored.tf": 'terraform { required_version = "= 0.1" }',
}


class Scan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        for rel, text in FIXTURE.items():
            p = base / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(textwrap.dedent(text), encoding="utf-8")
        self.res = scan_ops.scan(base / "root")

    def tearDown(self):
        self.tmp.cleanup()

    def test_providers(self):
        names = {a.split("/", 1)[1] for a in self.res.providers}
        self.assertEqual(names, {"hashicorp/aws", "hashicorp/tls", "hashicorp/time",
                                 "hashicorp/cloudinit", "hashicorp/null",
                                 "hashicorp/kubernetes", "hashicorp/random"})
        aws = self.res.providers["registry.terraform.io/hashicorp/aws"]
        self.assertEqual(aws.constraints, {"~> 5.100", ">= 5.95", "< 6.0"})
        self.assertEqual(set(aws.locked.values()), {"5.100.0"})
        self.assertTrue(self.res.providers["registry.terraform.io/hashicorp/random"].implied)

    def test_roots_and_core(self):
        roots = [m.path.name for m in self.res.roots]
        self.assertEqual(roots, ["root"])          # eks is a referenced child
        self.assertEqual(self.res.core_constraints, [">= 1.10, < 2.0"])
        self.assertTrue(self.res.roots[0].has_backend)

    def test_warnings(self):
        w = "\n".join(self.res.warnings)
        self.assertIn("terraform-aws-modules/vpc/aws", w)   # non-vendored module
        self.assertIn("hashicorp/random", w)                 # implied provider

    def test_needs_resolve(self):
        roots = self.res.roots
        tls = self.res.providers["registry.terraform.io/hashicorp/tls"]
        self.assertEqual(scan_ops.versions_to_mirror(tls, roots), (set(), True))
        aws = self.res.providers["registry.terraform.io/hashicorp/aws"]
        self.assertEqual(scan_ops.versions_to_mirror(aws, roots), ({"5.100.0"}, False))


class Parser(unittest.TestCase):
    def test_lock_hashes(self):
        tree = hcl_lite.parse(textwrap.dedent(FIXTURE["root/.terraform.lock.hcl"]))
        b = tree.children("provider")[0]
        self.assertEqual(b.attrs["hashes"], ["h1:abc=", "zh:0123"])

    def test_raw_expressions_skipped(self):
        tree = hcl_lite.parse('a = merge(\n {x = 1},\n var.y\n)\nb = "ok"\n')
        self.assertIsInstance(tree.attrs["a"], hcl_lite.Raw)
        self.assertEqual(tree.attrs["b"], "ok")


class Hashes(unittest.TestCase):
    def test_h1_matches_dirhash(self):
        with tempfile.TemporaryDirectory() as d:
            z = Path(d) / "p.zip"
            with zipfile.ZipFile(z, "w") as zf:
                zf.writestr("terraform-provider-x_v1.0.0", b"binary")
                zf.writestr("LICENSE.txt", b"license")
            lines = "".join(f"{hashlib.sha256(c).hexdigest()}  {n}\n" for n, c in
                            sorted([("LICENSE.txt", b"license"),
                                    ("terraform-provider-x_v1.0.0", b"binary")]))
            want = "h1:" + base64.b64encode(hashlib.sha256(lines.encode()).digest()).decode()
            self.assertEqual(fetch_ops.h1_of_zip(z), want)

    def test_cli_config_never_direct(self):
        rc = bundle_ops.cli_config("/opt/x/providers")
        self.assertIn('path    = "/opt/x/providers"', rc)
        self.assertIn('exclude = ["*/*/*"]', rc)


if __name__ == "__main__":
    unittest.main()
