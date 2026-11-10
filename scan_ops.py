"""
scan_ops — walk a folder tree and work out everything Terraform will need to
download at `terraform init` time: the Terraform core version and every
provider (source address + version constraints + exact locked versions).

What counts as what:
  * module dir — any directory holding *.tf / *.tf.json files.
  * root       — a module dir that has a .terraform.lock.hcl, OR that no other
                 scanned dir references as a local `module { source = "./..." }`.
                 Roots are where `terraform init` / `providers lock` run.
  * providers  — from `terraform { required_providers { ... } }`, plus any
                 provider used implicitly (a `provider "x"` block or an
                 `x_thing` resource/data source with no required_providers
                 entry), which Terraform resolves to hashicorp/x.
  * versions   — exact versions from each root's lock file win; constraints
                 from every module apply on top.

Also flags things that will still break offline: module sources that point at
a registry / git / http instead of a vendored local path.

Standard library only.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import hcl_lite
from tf_versions import allows_all

DEFAULT_HOST = "registry.terraform.io"
LOCK_NAME = ".terraform.lock.hcl"

# Never descend into these: provider/module caches, VCS metadata, our output.
_SKIP_DIRS = {".terraform", ".git", ".hg", ".svn", "node_modules", "__pycache__",
              ".venv", "venv", ".terragrunt-cache"}
# Local names that are Terraform built-ins, not downloadable providers.
_BUILTIN = {"terraform"}


@dataclass
class ProviderReq:
    address: str                 # registry.terraform.io/hashicorp/aws
    constraints: set[str] = field(default_factory=set)
    locked: dict[str, str] = field(default_factory=dict)   # root -> version
    used_by: set[str] = field(default_factory=set)          # module dirs
    implied: bool = False        # no required_providers entry anywhere

    @property
    def host(self):
        return self.address.split("/")[0]

    @property
    def namespace(self):
        return self.address.split("/")[1]

    @property
    def type(self):
        return self.address.split("/")[2]

    @property
    def short(self):
        h, ns, t = self.address.split("/")
        return f"{ns}/{t}" if h == DEFAULT_HOST else self.address


@dataclass
class ModuleDir:
    path: Path
    files: list[Path] = field(default_factory=list)
    required_versions: list[str] = field(default_factory=list)
    providers: dict[str, str] = field(default_factory=dict)   # local name -> address
    constraints: dict[str, list[str]] = field(default_factory=dict)  # address -> constraints
    local_module_sources: list[Path] = field(default_factory=list)
    has_backend: bool = False
    is_root: bool = False
    lock_file: Path | None = None
    locked: dict[str, dict] = field(default_factory=dict)     # address -> lock entry


@dataclass
class ScanResult:
    root: Path
    modules: list[ModuleDir]
    providers: dict[str, ProviderReq]
    core_constraints: list[str]
    warnings: list[str]

    @property
    def roots(self) -> list[ModuleDir]:
        return [m for m in self.modules if m.is_root]


def normalize_source(source: str) -> str:
    """'hashicorp/aws' -> 'registry.terraform.io/hashicorp/aws'; 'aws' ->
    'registry.terraform.io/hashicorp/aws'; full addresses pass through."""
    parts = [p for p in source.strip().lower().split("/") if p]
    if len(parts) == 1:
        parts = [DEFAULT_HOST, "hashicorp", parts[0]]
    elif len(parts) == 2:
        parts = [DEFAULT_HOST] + parts
    if len(parts) != 3:
        raise ValueError(f"bad provider source {source!r}")
    return "/".join(parts)


def _iter_tf_files(top: Path):
    for p in sorted(top.rglob("*")):
        if not p.is_file():
            continue
        if any(part in _SKIP_DIRS for part in p.relative_to(top).parts[:-1]):
            continue
        name = p.name
        if name.endswith(".tf") or name.endswith(".tf.json") or name == LOCK_NAME:
            yield p


def _is_local_source(src: str) -> bool:
    return src.startswith("./") or src.startswith("../") or src in (".", "..")


# -- per-file extraction -----------------------------------------------------
def _scan_hcl_file(md: ModuleDir, path: Path, warnings: list[str],
                   implied_uses: set[str]):
    try:
        tree = hcl_lite.parse(path.read_text(encoding="utf-8", errors="replace"))
    except hcl_lite.HCLError as e:
        warnings.append(f"{path}: could not parse ({e}); skipped")
        return
    _extract(md, path, tree.blocks, warnings, implied_uses)


def _scan_json_file(md: ModuleDir, path: Path, warnings: list[str],
                    implied_uses: set[str]):
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        warnings.append(f"{path}: could not parse JSON ({e}); skipped")
        return
    blocks = []
    tf = doc.get("terraform")
    for t in (tf if isinstance(tf, list) else [tf] if tf else []):
        b = hcl_lite.Block("terraform", [], {k: v for k, v in t.items()
                                               if k == "required_version"})
        for rp in _as_list(t.get("required_providers")):
            b.blocks.append(hcl_lite.Block("required_providers", [], dict(rp)))
        if t.get("backend"):
            b.blocks.append(hcl_lite.Block("backend", list(t["backend"])[:1]))
        blocks.append(b)
    for name in (doc.get("provider") or {}):
        blocks.append(hcl_lite.Block("provider", [name]))
    for kind in ("resource", "data", "ephemeral"):
        for rtype in (doc.get(kind) or {}):
            blocks.append(hcl_lite.Block(kind, [rtype]))
    for mname, m in (doc.get("module") or {}).items():
        for mm in _as_list(m):
            blocks.append(hcl_lite.Block("module", [mname], {"source": mm.get("source")}))
    _extract(md, path, blocks, warnings, implied_uses)


def _as_list(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _extract(md: ModuleDir, path: Path, blocks, warnings, implied_uses):
    for b in blocks:
        if b.type == "terraform":
            rv = b.attrs.get("required_version")
            if isinstance(rv, str):
                md.required_versions.append(rv)
            elif rv is not None:
                warnings.append(f"{path}: required_version is not a literal string")
            for rp in b.children("required_providers"):
                for local, spec in rp.attrs.items():
                    _add_required(md, path, local, spec, warnings)
            if b.children("backend") or b.children("cloud"):
                md.has_backend = True
        elif b.type == "provider" and b.labels:
            implied_uses.add(b.labels[0])
        elif b.type in ("resource", "data", "ephemeral") and b.labels:
            prov = b.attrs.get("provider")
            if isinstance(prov, hcl_lite.Raw) and prov.text:
                implied_uses.add(prov.text.split(".")[0].strip())
            else:
                implied_uses.add(b.labels[0].split("_", 1)[0])
        elif b.type == "module":
            src = b.attrs.get("source")
            if not isinstance(src, str):
                continue
            if _is_local_source(src):
                md.local_module_sources.append((md.path / src).resolve())
            else:
                warnings.append(
                    f"{path}: module \"{b.labels[0] if b.labels else '?'}\" source "
                    f"{src!r} is not a local path — `terraform init` will try to "
                    "download it. Vendor it into the repo (and point source at "
                    "the local copy) before crossing the air gap.")


# Things required_providers may carry that aren't provider entries.
_NOT_PROVIDERS = {"configuration_aliases"}


def _add_required(md, path, local, spec, warnings):
    if local in _NOT_PROVIDERS:
        return
    if isinstance(spec, str):                  # legacy:  aws = "~> 5.0"
        source, version = local, spec
    elif isinstance(spec, dict):
        source = spec.get("source") if isinstance(spec.get("source"), str) else local
        version = spec.get("version") if isinstance(spec.get("version"), str) else ""
    else:
        warnings.append(f"{path}: required_providers.{local} is not a literal; skipped")
        return
    try:
        addr = normalize_source(source)
    except ValueError as e:
        warnings.append(f"{path}: {e}")
        return
    md.providers[local] = addr
    cons = md.constraints.setdefault(addr, [])
    for c in version.split(","):
        if c.strip():
            cons.append(c.strip())


# -- lock files --------------------------------------------------------------
def parse_lock_file(path: Path) -> dict[str, dict]:
    """address -> {version, constraints, hashes}."""
    tree = hcl_lite.parse(path.read_text(encoding="utf-8"))
    out = {}
    for b in tree.children("provider"):
        if not b.labels:
            continue
        addr = normalize_source(b.labels[0])
        out[addr] = {
            "version": b.attrs.get("version") if isinstance(b.attrs.get("version"), str) else "",
            "constraints": b.attrs.get("constraints") if isinstance(b.attrs.get("constraints"), str) else "",
            "hashes": [h for h in (b.attrs.get("hashes") or []) if isinstance(h, str)],
        }
    return out


# -- the scan ----------------------------------------------------------------
def scan(top: Path) -> ScanResult:
    top = top.resolve()
    warnings: list[str] = []
    mods: dict[Path, ModuleDir] = {}
    implied: dict[Path, set[str]] = {}

    for f in _iter_tf_files(top):
        d = f.parent
        md = mods.setdefault(d, ModuleDir(d))
        if f.name == LOCK_NAME:
            md.lock_file = f
            try:
                md.locked = parse_lock_file(f)
            except (hcl_lite.HCLError, ValueError, OSError) as e:
                warnings.append(f"{f}: could not parse lock file ({e})")
            continue
        md.files.append(f)
        uses = implied.setdefault(d, set())
        if f.name.endswith(".tf.json"):
            _scan_json_file(md, f, warnings, uses)
        else:
            _scan_hcl_file(md, f, warnings, uses)

    # A lock file alone (no .tf) isn't a module; drop such dirs.
    mods = {d: m for d, m in mods.items() if m.files}

    referenced = {p for m in mods.values() for p in m.local_module_sources}
    for d, m in mods.items():
        m.is_root = bool(m.lock_file) or d.resolve() not in referenced

    providers: dict[str, ProviderReq] = {}
    for d, m in mods.items():
        for addr, cons in m.constraints.items():
            req = providers.setdefault(addr, ProviderReq(addr))
            req.used_by.add(str(d))
            req.constraints.update(cons)
        # Implicit providers: used but never declared in this module.
        declared = set(m.providers)
        for local in sorted(implied.get(d, set()) - declared - _BUILTIN):
            if not re.fullmatch(r"[a-z0-9-]+", local):
                continue
            addr = normalize_source(local)
            m.providers[local] = addr
            req = providers.get(addr)
            if req is None:
                req = providers[addr] = ProviderReq(addr, implied=True)
            req.used_by.add(str(d))

    for d, m in mods.items():
        for addr, entry in m.locked.items():
            req = providers.setdefault(addr, ProviderReq(addr))
            if entry.get("version"):
                req.locked[str(d)] = entry["version"]
            req.used_by.add(str(d))

    for req in providers.values():
        if req.implied and not req.constraints:
            warnings.append(
                f"{req.short}: used without a required_providers entry — "
                f"Terraform will assume {req.address}. Add an explicit entry "
                "if that's not right.")
        for root, v in req.locked.items():
            try:
                if not allows_all(req.constraints, v):
                    warnings.append(
                        f"{req.short}: lock in {root} pins {v}, which does not "
                        f"satisfy {', '.join(sorted(req.constraints))}")
            except ValueError as e:
                warnings.append(f"{req.short}: {e}")

    core = sorted({c for m in mods.values() for c in m.required_versions})
    ordered = sorted(mods.values(), key=lambda m: str(m.path).lower())
    return ScanResult(top, ordered, dict(sorted(providers.items())), core, warnings)


def versions_to_mirror(req: ProviderReq, roots: list[ModuleDir]) -> tuple[set[str], bool]:
    """(exact versions from lock files, needs_resolve). needs_resolve is True
    when some root that uses this provider has no lock entry for it, so the
    newest version matching the constraints must also be fetched."""
    exact = set(req.locked.values())
    needs = False
    for r in roots:
        if str(r.path) in req.used_by and req.address not in r.locked:
            needs = True
    # A provider used only by a child module still needs a version for the
    # root(s) that call it; if no root has it locked, resolve.
    if not exact:
        needs = True
    return exact, needs
