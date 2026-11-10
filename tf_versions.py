"""
tf_versions — Terraform-style version constraints, standard library only.

Implements the constraint syntax Terraform uses for both `required_version` and
provider `version` arguments:

    "1.2.3"  "= 1.2.3"  "!= 1.2.3"  "> 1.2"  ">= 1.10"  "< 6.0"  "<= 5.9"
    "~> 5.100"   (>= 5.100.0, < 6.0.0)
    "~> 5.100.1" (>= 5.100.1, < 5.101.0)
    multiple constraints are comma-separated and ALL must hold.

Pre-releases (1.11.0-beta1) only ever match a constraint that names that exact
pre-release, which is also Terraform's rule. That keeps "newest matching" from
ever picking an alpha/beta/rc.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import total_ordering

_VER_RE = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:-([0-9A-Za-z.\-]+))?(?:\+[0-9A-Za-z.\-]+)?$")
_CON_RE = re.compile(r"^\s*(~>|>=|<=|!=|=|>|<)?\s*(\S+)\s*$")


@total_ordering
@dataclass(frozen=True)
class Version:
    major: int
    minor: int
    patch: int
    pre: str = ""
    # How many numeric segments were written (for ~> semantics): "5.100" -> 2.
    given: int = 3

    @classmethod
    def parse(cls, text: str) -> "Version":
        m = _VER_RE.match(text.strip())
        if not m:
            raise ValueError(f"not a version: {text!r}")
        given = sum(1 for g in m.groups()[:3] if g is not None)
        return cls(int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0),
                   m.group(4) or "", given)

    @property
    def is_prerelease(self) -> bool:
        return bool(self.pre)

    def _key(self):
        # A release sorts after any of its pre-releases.
        return (self.major, self.minor, self.patch, 0 if self.pre else 1,
                _pre_key(self.pre))

    def __eq__(self, other):
        return isinstance(other, Version) and self._key() == other._key()

    def __hash__(self):
        return hash(self._key())

    def __lt__(self, other: "Version"):
        return self._key() < other._key()

    def __str__(self):
        s = f"{self.major}.{self.minor}.{self.patch}"
        return f"{s}-{self.pre}" if self.pre else s


def _pre_key(pre: str):
    out = []
    for part in pre.split(".") if pre else []:
        out.append((0, int(part), "") if part.isdigit() else (1, 0, part))
    return tuple(out)


@dataclass(frozen=True)
class Constraint:
    op: str
    version: Version

    def allows(self, v: Version) -> bool:
        c = self.version
        if v.is_prerelease and not (self.op in ("=", "") and v == c):
            return False
        if self.op in ("", "="):
            return v == c
        if self.op == "!=":
            return v != c
        if self.op == ">":
            return v > c
        if self.op == ">=":
            return v >= c
        if self.op == "<":
            return v < c
        if self.op == "<=":
            return v <= c
        if self.op == "~>":
            if v < c:
                return False
            if c.given <= 1:          # go-version: "~> 1" only means ">= 1"
                return True
            if c.given == 2:
                upper = Version(c.major + 1, 0, 0)
            else:
                upper = Version(c.major, c.minor + 1, 0)
            return v < upper
        raise ValueError(f"unknown operator {self.op!r}")

    def __str__(self):
        return f"{self.op} {self.version}".strip() if self.op else str(self.version)


def parse_constraints(text: str) -> list[Constraint]:
    """'>= 1.10, < 2.0' -> [Constraint('>=', 1.10.0), Constraint('<', 2.0.0)]."""
    out = []
    for piece in (text or "").split(","):
        if not piece.strip():
            continue
        m = _CON_RE.match(piece)
        if not m:
            raise ValueError(f"bad constraint: {piece!r}")
        out.append(Constraint(m.group(1) or "=", Version.parse(m.group(2))))
    return out


def allows_all(constraint_strings, version: Version | str) -> bool:
    v = Version.parse(version) if isinstance(version, str) else version
    for s in constraint_strings:
        for c in parse_constraints(s):
            if not c.allows(v):
                return False
    return True


def newest_matching(versions, constraint_strings) -> str | None:
    """Newest version string in `versions` satisfying every constraint string.
    Pre-releases are skipped unless a constraint pins them exactly."""
    best = None
    for raw in versions:
        try:
            v = Version.parse(raw)
        except ValueError:
            continue
        if v.is_prerelease and not any(
                c.op in ("=", "") and c.version == v
                for s in constraint_strings for c in parse_constraints(s)):
            continue
        if allows_all(constraint_strings, v) and (best is None or v > best[0]):
            best = (v, raw)
    return best[1] if best else None
