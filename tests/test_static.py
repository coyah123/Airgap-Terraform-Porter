"""Source checks that catch bugs the GUI only shows on an error path.

    python -m unittest discover -s tests
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ExceptLambdas(unittest.TestCase):
    """`except X as e: self.after(0, lambda: f(e))` raises NameError ("cannot
    access free variable 'e'") when the lambda finally runs, because Python
    deletes `e` at the end of the except block. Bind it: `lambda err=e: f(err)`."""

    def test_no_lambda_captures_except_name(self):
        bad = []
        for src in sorted(ROOT.glob("*.py")):
            tree = ast.parse(src.read_text(encoding="utf-8"))
            for h in ast.walk(tree):
                if not (isinstance(h, ast.ExceptHandler) and h.name):
                    continue
                for node in ast.walk(h):
                    if not isinstance(node, ast.Lambda):
                        continue
                    params = {a.arg for a in node.args.args + node.args.kwonlyargs}
                    uses = {n.id for n in ast.walk(node.body) if isinstance(n, ast.Name)}
                    if h.name in uses and h.name not in params:
                        bad.append(f"{src.name}:{node.lineno}")
        self.assertEqual(bad, [], "lambda uses the except variable after it's deleted")


if __name__ == "__main__":
    unittest.main()
