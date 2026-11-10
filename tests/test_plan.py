"""Plan Explorer engine tests, from real `terraform show -json` output of the
offline fixture in tests/fixtures/plan_stack (built-in terraform_data only).

    python -m unittest discover -s tests
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import plan_ops  # noqa: E402


def load(name):
    return json.loads((HERE / "fixtures" / name).read_text(encoding="utf-8"))


def find(node, pred):
    if pred(node):
        return node
    for c in node.children:
        f = find(c, pred)
        if f:
            return f
    return None


class CreatePlan(unittest.TestCase):
    def setUp(self):
        self.plan = load("plan_create.json")
        self.root = plan_ops.build_tree(self.plan, "stack")

    def test_summary(self):
        self.assertEqual(plan_ops.summary_line(self.plan),
                         "8 to add, 0 to change, 0 to destroy")

    def test_tree_shape(self):
        kids = {(c.kind, c.label) for c in self.root.children}
        self.assertIn(("group", "terraform_data"), kids)          # subnet ×3
        self.assertIn(("module", 'module.app["dev"]'), kids)
        self.assertIn(("module", "outputs"), kids)
        grp = find(self.root, lambda n: n.kind == "group")
        self.assertEqual(len(grp.children), 3)
        self.assertEqual(grp.refs, ["terraform_data.network"])
        self.assertEqual(self.root.action, "create")

    def test_sensitive_and_unknown(self):
        sec = find(self.root, lambda n: n.address.endswith('["dev"].terraform_data.secret'))
        rows = {r[0]: r for r in plan_ops.attribute_rows(sec.change)}
        self.assertEqual(rows["input"][2], "(sensitive)")
        self.assertEqual(rows["input"][1], "")                     # nothing before
        self.assertEqual(rows["id"][2], "(known after apply)")


class ChangePlan(unittest.TestCase):
    def setUp(self):
        self.plan = load("plan_changes.json")
        self.root = plan_ops.build_tree(self.plan, "stack")

    def test_actions_and_reasons(self):
        self.assertEqual(plan_ops.summary_line(self.plan),
                         "2 to add, 1 to change, 3 to destroy")
        net = find(self.root, lambda n: n.address == "terraform_data.network")
        self.assertEqual(net.action, "update")
        rows = {r[0]: r for r in plan_ops.attribute_rows(net.change)}
        self.assertEqual(rows["input.tag"][1:], ("v1", "v2", True))
        gone = find(self.root, lambda n: n.address == "terraform_data.subnet[2]")
        self.assertEqual((gone.action, gone.reason), ("delete", "delete_because_count_index"))
        prod = find(self.root, lambda n: n.label == 'module.app["prod"]')
        self.assertEqual(prod.action, "delete")
        self.assertEqual(self.root.action, "delete")               # worst wins

    def test_filters(self):
        r = plan_ops.build_tree(self.plan, "s", show_noop=False)
        self.assertIsNone(find(r, lambda n: n.address == "terraform_data.subnet[0]"))
        self.assertIsNone(find(r, lambda n: n.label == 'module.app["dev"]'))


class Helpers(unittest.TestCase):
    def test_action_key(self):
        self.assertEqual(plan_ops.action_key(["delete", "create"]), "replace")
        self.assertEqual(plan_ops.action_key(["create", "delete"]), "replace")
        self.assertEqual(plan_ops.action_key(["no-op"]), "no-op")
        self.assertEqual(plan_ops.action_key(["read"]), "read")

    def test_ref_target(self):
        self.assertEqual(plan_ops._ref_target("aws_vpc.this.id"), "aws_vpc.this")
        self.assertEqual(plan_ops._ref_target("module.vpc.ids"), "module.vpc")
        self.assertEqual(plan_ops._ref_target("data.aws_x.y.json"), "data.aws_x.y")
        self.assertEqual(plan_ops._ref_target("var.region"), "")

    def test_override_never_clobbers(self):
        with tempfile.TemporaryDirectory() as d:
            st = Path(d)
            (st / "main.tf").write_text("")
            cfg = plan_ops.RunConfig(st, "terraform")
            with plan_ops.LocalBackend(cfg):
                self.assertTrue((st / plan_ops.OVERRIDE_NAME).exists())
            self.assertFalse((st / plan_ops.OVERRIDE_NAME).exists())
            (st / plan_ops.OVERRIDE_NAME).write_text("terraform {}\n")   # user's own
            with self.assertRaises(RuntimeError):
                with plan_ops.LocalBackend(cfg):
                    pass
            self.assertEqual((st / plan_ops.OVERRIDE_NAME).read_text(), "terraform {}\n")


class ApplyGuards(unittest.TestCase):
    """apply_blockers: the rules that decide whether Apply is offered."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = Path(self.tmp.name) / "stack"
        self.st.mkdir()
        (self.st / "main.tf").write_text('resource "terraform_data" "x" {}\n')
        self.changes = load("plan_changes.json")

    def tearDown(self):
        self.tmp.cleanup()

    def cfg(self, local=True):
        c = plan_ops.RunConfig(self.st, "terraform", local_backend=local)
        c.plan_file.parent.mkdir(parents=True, exist_ok=True)
        c.plan_file.write_bytes(b"x")
        self.addCleanup(lambda: c.plan_file.unlink(missing_ok=True))
        return c

    def test_ok_for_stack_without_backend(self):
        c = self.cfg()
        self.assertEqual(plan_ops.apply_blockers(c, self.changes,
                                                 plan_ops.input_fingerprint(c)), [])

    def test_refuses_temp_backend_on_real_backend_stack(self):
        (self.st / "backend.tf").write_text('terraform {\n  backend "s3" {}\n}\n')
        c = self.cfg(local=True)
        why = plan_ops.apply_blockers(c, self.changes, plan_ops.input_fingerprint(c))
        self.assertTrue(any("throwaway state" in w for w in why))
        c2 = self.cfg(local=False)                 # real backend -> allowed
        self.assertEqual(plan_ops.apply_blockers(c2, self.changes,
                                                 plan_ops.input_fingerprint(c2)), [])

    def test_refuses_stale_and_empty_and_missing(self):
        c = self.cfg()
        fp = plan_ops.input_fingerprint(c)
        import os
        t = (self.st / "main.tf").stat().st_mtime + 5
        os.utime(self.st / "main.tf", (t, t))
        self.assertTrue(any("changed after" in w for w in
                            plan_ops.apply_blockers(c, self.changes, fp)))
        nothing = load("plan_changes.json")
        for rc in nothing["resource_changes"]:
            rc["change"]["actions"] = ["no-op"]
        nothing["output_changes"] = {}
        self.assertTrue(any("no changes" in w for w in plan_ops.apply_blockers(
            c, nothing, plan_ops.input_fingerprint(c))))
        c.plan_file.unlink()
        self.assertTrue(any("No saved plan" in w for w in plan_ops.apply_blockers(
            c, self.changes, plan_ops.input_fingerprint(c))))

    def test_risky_list(self):
        self.assertEqual(len(plan_ops.risky_changes(self.changes)), 3)


if __name__ == "__main__":
    unittest.main()
