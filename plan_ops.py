"""
plan_ops — drive `terraform init / validate / plan / show / apply` for one
stack and turn the results into a tree the Plan Explorer can draw.

init, validate, plan and show are read-only: a plan only *reads* the target
account (refresh + data sources). Apply is separate and guarded (see
apply_blockers): it only ever applies the saved plan file that was reviewed,
never with a temporary no-state backend on a stack that has a real one, and
never when the stack's files changed after the plan was made.

Run options (RunConfig):
  * terraform binary — PATH, or a bundle's bin/<os>_<arch>/terraform.
  * providers — normal CLI config, or a bundle folder (offline: init gets
    -plugin-dir=<bundle>/providers, so it never contacts a registry).
  * backend — the stack's configured backend (with an optional
    -backend-config file), or "local, no state": a temporary, clearly marked
    backend_override.tf plus a private TF_DATA_DIR, so no state bucket is
    needed and the stack's real .terraform is left alone. The override is
    always removed afterwards. With no state, everything plans as "create".
  * var files — passed as -var-file=..., one per file.

Machine-readable sources:
  * `terraform validate -json`         -> diagnostics
  * `terraform plan -json -out=FILE`   -> streamed UI events (diagnostics,
                                          change summary, progress)
  * `terraform show -json FILE`        -> the full plan document

Standard library only.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

OVERRIDE_NAME = "backend_override.tf"
OVERRIDE_MARK = "# airgap-terraform-porter: temporary local backend (safe to delete)"
OVERRIDE_TEXT = OVERRIDE_MARK + "\nterraform {\n  backend \"local\" {}\n}\n"


def aws_profiles() -> list[str]:
    """Profile names from ~/.aws/config and ~/.aws/credentials (no secrets read
    beyond section headers)."""
    names = set()
    home = Path(os.environ.get("AWS_CONFIG_FILE") or Path.home() / ".aws" / "config")
    cred = Path(os.environ.get("AWS_SHARED_CREDENTIALS_FILE")
                or Path.home() / ".aws" / "credentials")
    for f, prefix in ((home, "profile "), (cred, "")):
        try:
            for ln in f.read_text(encoding="utf-8", errors="replace").splitlines():
                ln = ln.strip()
                if ln.startswith("[") and ln.endswith("]"):
                    n = ln[1:-1].strip()
                    if n.startswith(prefix):
                        n = n[len(prefix):]
                    if n and not n.startswith(("sso-session", "services")):
                        names.add(n)
        except OSError:
            pass
    return sorted(names)


def cache_root() -> Path:
    """Per-user cache: %LOCALAPPDATA% (Windows), ~/Library/Caches (macOS),
    $XDG_CACHE_HOME or ~/.cache (Linux)."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "airgap-terraform-porter"


@dataclass
class RunConfig:
    stack: Path
    terraform: str
    bundle: Path | None = None          # use <bundle>/providers offline
    local_backend: bool = True
    backend_config: str = ""            # file for -backend-config (configured backend)
    var_files: list[str] = field(default_factory=list)
    aws_profile: str = ""               # -> AWS_PROFILE for the provider

    @property
    def work_id(self) -> str:
        return hashlib.sha1(str(self.stack.resolve()).lower().encode()).hexdigest()[:10]

    @property
    def data_dir(self) -> Path | None:
        """Private TF_DATA_DIR in local-backend mode (keeps the real
        .terraform untouched); None = use the stack's own .terraform."""
        if self.local_backend:
            return cache_root() / "work" / f"{self.stack.name}-{self.work_id}"
        return None

    @property
    def plan_file(self) -> Path:
        return cache_root() / "plans" / f"{self.stack.name}-{self.work_id}.tfplan"

    def env(self) -> dict:
        env = dict(os.environ)
        env["TF_IN_AUTOMATION"] = "1"
        env["TF_INPUT"] = "0"
        env.pop("TF_CLI_ARGS", None)
        env.pop("TF_CLI_ARGS_init", None)
        env.pop("TF_CLI_ARGS_plan", None)
        if self.data_dir:
            env["TF_DATA_DIR"] = str(self.data_dir)
        if self.aws_profile:
            env["AWS_PROFILE"] = self.aws_profile
            env.pop("AWS_ACCESS_KEY_ID", None)
            env.pop("AWS_SECRET_ACCESS_KEY", None)
            env.pop("AWS_SESSION_TOKEN", None)
        return env


# -- argv ---------------------------------------------------------------------
def init_argv(cfg: RunConfig) -> list[str]:
    argv = [cfg.terraform, "init", "-input=false", "-no-color"]
    if cfg.bundle:
        argv.append(f"-plugin-dir={(cfg.bundle / 'providers').resolve()}")
    if cfg.local_backend:
        argv.append("-reconfigure")
    elif cfg.backend_config:
        argv.append(f"-backend-config={cfg.backend_config}")
    return argv


def validate_argv(cfg: RunConfig) -> list[str]:
    return [cfg.terraform, "validate", "-json", "-no-color"]


def plan_argv(cfg: RunConfig) -> list[str]:
    argv = [cfg.terraform, "plan", "-json", "-input=false", "-no-color",
            f"-out={cfg.plan_file}"]
    argv += [f"-var-file={v}" for v in cfg.var_files]
    return argv


def show_argv(cfg: RunConfig, plan_file: Path | None = None) -> list[str]:
    return [cfg.terraform, "show", "-json", "-no-color", str(plan_file or cfg.plan_file)]


def display(argv: list[str]) -> str:
    return " ".join(f'"{a}"' if " " in a else a for a in argv)


# -- running ------------------------------------------------------------------
def _kw():
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def run_stream(argv, cfg: RunConfig, on_line) -> int:
    proc = subprocess.Popen(argv, cwd=str(cfg.stack), env=cfg.env(),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", **_kw())
    for line in proc.stdout:
        on_line(line.rstrip("\n"))
    return proc.wait()


def run_capture(argv, cfg: RunConfig) -> tuple[int, str, str]:
    p = subprocess.run(argv, cwd=str(cfg.stack), env=cfg.env(), capture_output=True,
                       text=True, encoding="utf-8", errors="replace", **_kw())
    return p.returncode, p.stdout, p.stderr


class LocalBackend:
    """Context manager: drop the marked backend_override.tf in, always remove it.
    Refuses to touch a backend_override.tf it didn't write."""

    def __init__(self, cfg: RunConfig):
        self.cfg = cfg
        self.path = cfg.stack / OVERRIDE_NAME
        self.wrote = False

    def __enter__(self):
        if not self.cfg.local_backend:
            return self
        if self.path.exists():
            text = self.path.read_text(encoding="utf-8", errors="replace")
            if OVERRIDE_MARK not in text:
                raise RuntimeError(
                    f"{self.path} already exists and isn't ours — not overwriting it. "
                    "Remove it, or untick 'local backend'.")
        self.path.write_text(OVERRIDE_TEXT, encoding="utf-8", newline="\n")
        self.wrote = True
        return self

    def __exit__(self, *exc):
        if self.wrote:
            try:
                self.path.unlink()
            except OSError:
                pass
        return False


@dataclass
class Diagnostic:
    severity: str           # error | warning
    summary: str
    detail: str = ""
    where: str = ""         # file:line
    address: str = ""

    @classmethod
    def from_json(cls, d: dict) -> "Diagnostic":
        rng = d.get("range") or {}
        where = ""
        if rng.get("filename"):
            where = f"{rng['filename']}:{(rng.get('start') or {}).get('line', '?')}"
        return cls(d.get("severity", "error"), d.get("summary", ""),
                   d.get("detail", ""), where, d.get("address", ""))


@dataclass
class StageResult:
    name: str               # init | validate | plan
    ok: bool
    diagnostics: list[Diagnostic] = field(default_factory=list)
    note: str = ""


def run_init(cfg: RunConfig, on_line) -> StageResult:
    if cfg.data_dir:
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []

    def keep(ln):
        lines.append(ln)
        on_line(ln)
    code = run_stream(init_argv(cfg), cfg, keep)
    diags = []
    if code != 0:
        diags = _text_errors(lines) or [Diagnostic("error", "terraform init failed",
                                                   "\n".join(lines[-15:]))]
    return StageResult("init", code == 0, diags)


def _text_errors(lines: list[str]) -> list[Diagnostic]:
    """Pull 'Error: <summary>' blocks out of plain (non-JSON) output."""
    out, cur = [], None
    for ln in lines:
        s = ln.strip().lstrip("│╷╵ ").strip()
        if s.startswith("Error: "):
            cur = Diagnostic("error", s[7:])
            out.append(cur)
        elif s.startswith("Warning: "):
            cur = Diagnostic("warning", s[9:])
            out.append(cur)
        elif cur is not None and s:
            if s.startswith("on ") and " line " in s and not cur.where:
                cur.where = s[3:].split(",")[0].replace(" line ", ":")
            else:
                cur.detail = (cur.detail + "\n" + s).strip()
    return out


def run_validate(cfg: RunConfig, on_line) -> StageResult:
    code, out, err = run_capture(validate_argv(cfg), cfg)
    try:
        doc = json.loads(out)
    except ValueError:
        for ln in (out + err).splitlines():
            on_line(ln)
        return StageResult("validate", False, _text_errors((out + err).splitlines()))
    diags = [Diagnostic.from_json(d) for d in doc.get("diagnostics", [])]
    for d in diags:
        on_line(f"{d.severity}: {d.summary}" + (f"  ({d.where})" if d.where else ""))
    ok = bool(doc.get("valid")) and code == 0
    on_line("configuration is valid" if ok else "configuration is NOT valid")
    return StageResult("validate", ok, diags,
                       f"{doc.get('warning_count', 0)} warning(s)")


def run_plan(cfg: RunConfig, on_line) -> tuple[StageResult, dict | None]:
    """Plan to a file, stream the -json UI events, then `show -json` it."""
    cfg.plan_file.parent.mkdir(parents=True, exist_ok=True)
    if cfg.plan_file.exists():
        cfg.plan_file.unlink()
    diags: list[Diagnostic] = []
    summary = {}

    def event(ln):
        try:
            ev = json.loads(ln)
        except ValueError:
            on_line(ln)
            return
        t = ev.get("type")
        if t == "diagnostic":
            diags.append(Diagnostic.from_json(ev.get("diagnostic") or {}))
        elif t == "change_summary":
            summary.update(ev.get("changes") or {})
        if t not in ("version",):
            on_line(ev.get("@message", ln))
    code = run_stream(plan_argv(cfg), cfg, event)
    if code != 0 or not cfg.plan_file.exists():
        if not diags:
            diags = [Diagnostic("error", "terraform plan failed (see output)")]
        return StageResult("plan", False, diags), None
    c2, out, err = run_capture(show_argv(cfg), cfg)
    if c2 != 0:
        return StageResult("plan", False, diags + [
            Diagnostic("error", "terraform show -json failed", err.strip())]), None
    plan = json.loads(out)
    note = ", ".join(f"{summary.get(k, 0)} to {k}" for k in ("add", "change", "remove")) \
        if summary else ""
    return StageResult("plan", True, diags, note), plan


def load_plan_file(path: Path, terraform: str) -> dict:
    """Open a saved plan: a `show -json` document, or a binary .tfplan (shown
    with terraform from its own directory — needs that dir initialised)."""
    raw = path.read_bytes()
    if raw[:1] in (b"{", b" ", b"\n", b"\r", b"\t") or raw[:3] == b"\xef\xbb\xbf":
        return json.loads(raw.decode("utf-8-sig"))
    cfg = RunConfig(path.parent, terraform, local_backend=False)
    code, out, err = run_capture(show_argv(cfg, path), cfg)
    if code != 0:
        raise RuntimeError(err.strip() or "terraform show failed")
    return json.loads(out)


# -- plan -> tree ---------------------------------------------------------------
# action key -> (symbol, label, rank). Rank picks the "worst" for summaries.
ACTIONS = {
    "create":  ("+", "create", 2),
    "update":  ("~", "update", 3),
    "replace": ("±", "replace", 4),
    "delete":  ("-", "destroy", 5),
    "read":    ("≡", "read", 1),
    "no-op":   ("·", "no change", 0),
    "forget":  ("✂", "forget", 3),
}


def action_key(actions: list[str]) -> str:
    a = list(actions or ["no-op"])
    if "delete" in a and "create" in a:
        return "replace"
    return a[0] if a[0] in ACTIONS else "update"


def _flatten(v, prefix=""):
    """Nested dict/list -> {dotted.path: scalar}. Empty containers kept."""
    out = {}
    if isinstance(v, dict):
        if not v and prefix:
            out[prefix] = {}
        for k, x in v.items():
            out.update(_flatten(x, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(v, list):
        if not v and prefix:
            out[prefix] = []
        for i, x in enumerate(v):
            out.update(_flatten(x, f"{prefix}[{i}]"))
    else:
        out[prefix] = v
    return out


def _paths_true(v, prefix=""):
    """Paths whose marker value is true (after_unknown / *_sensitive)."""
    out = set()
    if v is True:
        out.add(prefix)
    elif isinstance(v, dict):
        for k, x in v.items():
            out |= _paths_true(x, f"{prefix}.{k}" if prefix else str(k))
    elif isinstance(v, list):
        for i, x in enumerate(v):
            out |= _paths_true(x, f"{prefix}[{i}]")
    return out


def _under(path: str, marks: set[str]) -> bool:
    return any(path == m or path.startswith(m + ".") or path.startswith(m + "[")
               for m in marks if m)


def _fmt(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, str):
        return v
    return json.dumps(v)


def attribute_rows(change: dict) -> list[tuple[str, str, str, bool]]:
    """(attribute, before, after, changed) for one resource change."""
    before = _flatten(change.get("before") or {})
    after = _flatten(change.get("after") or {})
    unknown = _paths_true(change.get("after_unknown") or {})
    sens = _paths_true(change.get("before_sensitive") or {}) | \
        _paths_true(change.get("after_sensitive") or {})
    if change.get("before_sensitive") is True or change.get("after_sensitive") is True:
        sens.add("")
    keys = sorted(set(before) | set(after) | unknown,
                  key=lambda k: (k.split(".")[0].split("[")[0], k))
    rows = []
    for k in keys:
        b = before.get(k, None)
        bs = "" if k not in before else (
            "(sensitive)" if _under(k, sens) or "" in sens else _fmt(b))
        if _under(k, unknown) and k not in after:
            a_s = "(known after apply)"
            changed = True
        else:
            a = after.get(k, None)
            a_s = "" if k not in after else (
                "(sensitive)" if _under(k, sens) or "" in sens else _fmt(a))
            changed = (k in before) != (k in after) or b != a
        rows.append((k, bs, a_s, changed))
    return rows


@dataclass
class Node:
    kind: str                 # root | module | group | resource | data | output
    label: str                # big text (type / module name)
    sub: str                  # small text (name / summary)
    action: str = "no-op"
    address: str = ""
    change: dict | None = None
    reason: str = ""
    children: list["Node"] = field(default_factory=list)
    refs: list[str] = field(default_factory=list)
    counts: dict = field(default_factory=dict)

    def worst(self) -> str:
        best = self.action
        for c in self.children:
            w = c.worst()
            if ACTIONS[w][2] > ACTIONS[best][2]:
                best = w
        return best


def _config_refs(plan: dict) -> dict[str, list[str]]:
    """Resource address (no index, with module path) -> references from config."""
    out: dict[str, list[str]] = {}

    def walk(mod: dict, prefix: str):
        for r in mod.get("resources", []) or []:
            addr = (prefix + "." if prefix else "") + r.get("address", "")
            refs = set(r.get("depends_on") or [])
            for e in _iter_exprs(r.get("expressions") or {}):
                refs.update(e)
            out[addr] = sorted({_ref_target(x) for x in refs} - {""})
        for name, call in (mod.get("module_calls") or {}).items():
            walk(call.get("module") or {}, (prefix + "." if prefix else "") + f"module.{name}")
    walk(((plan.get("configuration") or {}).get("root_module")) or {}, "")
    return out


def _ref_target(ref: str) -> str:
    """'terraform_data.network.id' -> 'terraform_data.network';
    'module.vpc.subnet_ids' -> 'module.vpc'; 'data.aws_x.y.id' -> 'data.aws_x.y'.
    Variables, locals, each/count/path/self aren't resources -> ''."""
    if ref.startswith(("var.", "local.", "each.", "count.", "path.", "self",
                       "terraform.")):
        return ""
    parts = _strip_index(ref).split(".")
    if parts[0] == "module":
        return ".".join(parts[:2])
    if parts[0] == "data":
        return ".".join(parts[:3])
    return ".".join(parts[:2])


def _iter_exprs(e):
    if isinstance(e, dict):
        if "references" in e and isinstance(e["references"], list):
            yield [r for r in e["references"] if isinstance(r, str)]
        for v in e.values():
            yield from _iter_exprs(v)
    elif isinstance(e, list):
        for v in e:
            yield from _iter_exprs(v)


def _strip_index(addr: str) -> str:
    """module.a["x"].aws_y.z[0] -> module.a.aws_y.z"""
    out, depth = [], 0
    for ch in addr:
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        elif depth == 0:
            out.append(ch)
    return "".join(out)


def build_tree(plan: dict, title: str, show_noop: bool = True,
               show_data: bool = True) -> Node:
    refs = _config_refs(plan)
    root = Node("root", title, "", "no-op")
    modules: dict[str, Node] = {"": root}

    def module_node(maddr: str) -> Node:
        if maddr in modules:
            return modules[maddr]
        parts = _split_module(maddr)
        parent = module_node(".".join(parts[:-1]) if len(parts) > 1 else "")
        n = Node("module", parts[-1], "", "no-op", address=maddr)
        parent.children.append(n)
        modules[maddr] = n
        return n

    groups: dict[str, Node] = {}
    counts = {k: 0 for k in ACTIONS}
    for rc in plan.get("resource_changes", []) or []:
        ch = rc.get("change") or {}
        act = action_key(ch.get("actions"))
        is_data = rc.get("mode") == "data"
        counts[act] = counts.get(act, 0) + 1
        if act == "no-op" and not show_noop:
            continue
        if is_data and not show_data:
            continue
        parent = module_node(rc.get("module_address", "") or "")
        kind = "data" if is_data else "resource"
        typ = ("data." if is_data else "") + rc.get("type", "")
        idx = rc.get("index")
        name = rc.get("name", "") + (f"[{json.dumps(idx)}]" if idx is not None else "")
        leaf = Node(kind, typ, name, act, rc.get("address", ""), ch,
                    rc.get("action_reason", ""),
                    refs=refs.get(_strip_index(rc.get("address", "")), []))
        if idx is not None:                     # count / for_each -> group them
            gkey = _strip_index(rc.get("address", ""))
            g = groups.get(gkey)
            if g is None:
                g = groups[gkey] = Node("group", typ, rc.get("name", ""), "no-op",
                                        gkey, refs=leaf.refs)
                parent.children.append(g)
            g.children.append(leaf)
        else:
            parent.children.append(leaf)
    for g in groups.values():
        g.sub = f"{g.sub}  ×{len(g.children)}"
        if len(g.children) == 1:            # count = 1 / single key: no group box
            parent_of = _find_parent(root, g)
            if parent_of is not None:
                i = parent_of.children.index(g)
                parent_of.children[i] = g.children[0]
    outs = plan.get("output_changes") or {}
    changed_outs = [(k, v) for k, v in sorted(outs.items())
                    if action_key((v or {}).get("actions")) != "no-op" or show_noop]
    if changed_outs:
        on = Node("module", "outputs", f"{len(changed_outs)} output(s)", "no-op")
        for k, v in changed_outs:
            on.children.append(Node("output", "output", k,
                                    action_key((v or {}).get("actions")), f"output.{k}", v))
        root.children.append(on)
    root.counts = counts
    _summarise(root)
    return root


def _find_parent(n: Node, target: Node):
    for c in n.children:
        if c is target:
            return n
        p = _find_parent(c, target)
        if p is not None:
            return p
    return None


def _summarise(n: Node):
    for c in n.children:
        _summarise(c)
    if n.kind in ("module", "group", "root"):
        tally = {}
        for leaf in _leaves(n):
            if n.kind == "root" and leaf.kind == "output":
                continue
            tally[leaf.action] = tally.get(leaf.action, 0) + 1
        bits = [f"{ACTIONS[a][0]}{tally[a]}" for a in
                ("create", "update", "replace", "delete", "read", "no-op") if tally.get(a)]
        if n.kind != "group":
            n.sub = (n.sub + "  " if n.sub else "") + " ".join(bits)
        n.action = n.worst() if n.children else "no-op"


def _leaves(n: Node):
    if n.kind in ("resource", "data", "output"):
        yield n
    for c in n.children:
        yield from _leaves(c)


def _split_module(maddr: str) -> list[str]:
    """'module.a.module.b["k"]' -> ['module.a', 'module.a.module.b["k"]'] style
    cumulative parts; returns the *labels* per level for display."""
    parts, cur, depth = [], "", 0
    tokens = []
    for ch in maddr:
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        if ch == "." and depth == 0:
            tokens.append(cur)
            cur = ""
        else:
            cur += ch
    tokens.append(cur)
    # tokens: ['module', 'a', 'module', 'b["k"]']
    for i in range(0, len(tokens) - 1, 2):
        parts.append(f"{tokens[i]}.{tokens[i + 1]}")
    return parts or [maddr]


def summary_line(plan: dict) -> str:
    c = {k: 0 for k in ACTIONS}
    for rc in plan.get("resource_changes", []) or []:
        c[action_key((rc.get("change") or {}).get("actions"))] += 1
    adds = c["create"] + c["replace"]
    dels = c["delete"] + c["replace"]
    s = f"{adds} to add, {c['update']} to change, {dels} to destroy"
    if c["replace"]:
        s += f" ({c['replace']} replaced)"
    if c["read"]:
        s += f"; {c['read']} data source(s) read at apply"
    if plan.get("errored"):
        s += "  — plan ERRORED (partial)"
    return s


# -- apply ------------------------------------------------------------------------
def apply_argv(cfg: RunConfig) -> list[str]:
    """Apply exactly the saved, reviewed plan. A saved plan needs no
    -auto-approve; Terraform refuses it if state moved on since the plan."""
    return [cfg.terraform, "apply", "-input=false", "-no-color", "-json",
            str(cfg.plan_file)]


def stack_has_backend(stack: Path) -> bool:
    """Does the root module declare a backend / cloud block (ignoring our own
    temporary override)?"""
    import hcl_lite
    for f in sorted(stack.glob("*.tf")):
        if f.name == OVERRIDE_NAME and OVERRIDE_MARK in f.read_text(
                encoding="utf-8", errors="replace"):
            continue
        try:
            tree = hcl_lite.parse(f.read_text(encoding="utf-8", errors="replace"))
        except hcl_lite.HCLError:
            continue
        for b in tree.children("terraform"):
            if b.children("backend") or b.children("cloud"):
                return True
    return False


def input_fingerprint(cfg: RunConfig) -> dict[str, float]:
    """mtimes of everything a plan depends on locally: every .tf / .tf.json in
    the tree (modules included, .terraform excluded), the lock file and the var
    files. Compared before apply to catch edits made after the plan."""
    out = {}
    for p in cfg.stack.rglob("*"):
        if ".terraform" in p.parts or not p.is_file():
            continue
        if p.name == OVERRIDE_NAME:
            continue
        if p.suffix == ".tf" or p.name.endswith(".tf.json") or p.name == ".terraform.lock.hcl":
            out[str(p)] = p.stat().st_mtime
    for v in cfg.var_files:
        vp = cfg.stack / v if not Path(v).is_absolute() else Path(v)
        if vp.is_file():
            out[str(vp)] = vp.stat().st_mtime
    return out


def apply_blockers(cfg: RunConfig, plan: dict | None, planned_fp: dict | None) -> list[str]:
    """Reasons apply must not run. Empty list = OK to offer apply."""
    why = []
    if plan is None or not cfg.plan_file.exists():
        why.append("No saved plan from this session — run a plan first.")
        return why
    if plan.get("errored"):
        why.append("The plan errored; fix the problems and re-plan.")
    if not plan_has_changes(plan):
        why.append("The plan has no changes — nothing to apply.")
    if cfg.local_backend and stack_has_backend(cfg.stack):
        why.append("This plan used the temporary 'local backend, no state'. Applying it "
                   "would create real resources with throwaway state. Untick 'local "
                   "backend', plan against the real backend, then apply.")
    if planned_fp is not None and input_fingerprint(cfg) != planned_fp:
        why.append("Stack files or var files changed after this plan — re-plan first.")
    return why


def plan_has_changes(plan: dict) -> bool:
    for rc in plan.get("resource_changes", []) or []:
        if action_key((rc.get("change") or {}).get("actions")) not in ("no-op", "read"):
            return True
    for v in (plan.get("output_changes") or {}).values():
        if action_key((v or {}).get("actions")) != "no-op":
            return True
    return False


def risky_changes(plan: dict) -> list[tuple[str, str]]:
    """(address, 'destroy'|'replace') for everything apply would delete."""
    out = []
    for rc in plan.get("resource_changes", []) or []:
        k = action_key((rc.get("change") or {}).get("actions"))
        if k in ("delete", "replace"):
            out.append((rc.get("address", ""), "destroy" if k == "delete" else "replace"))
    return out


def plan_accounts(plan: dict) -> list[str]:
    """Best-effort: the AWS account ids this plan is pinned to (allowed_account_ids
    variable or provider config), for the confirmation dialog."""
    vals = []
    v = ((plan.get("variables") or {}).get("allowed_account_ids") or {}).get("value")
    if isinstance(v, list):
        vals += [str(x) for x in v]
    return vals


def run_apply(cfg: RunConfig, on_line, on_resource) -> StageResult:
    """Stream `terraform apply -json <plan>`. on_resource(address, state) is
    called with state in {"applying", "done", "error"} as resources progress."""
    diags: list[Diagnostic] = []
    summary = {}

    def event(ln):
        try:
            ev = json.loads(ln)
        except ValueError:
            on_line(ln)
            return
        t = ev.get("type")
        res = ((ev.get("hook") or {}).get("resource") or {}).get("addr", "")
        if t == "apply_start" and res:
            on_resource(res, "applying")
        elif t == "apply_complete" and res:
            on_resource(res, "done")
        elif t == "apply_errored" and res:
            on_resource(res, "error")
        elif t == "diagnostic":
            diags.append(Diagnostic.from_json(ev.get("diagnostic") or {}))
        elif t == "change_summary":
            summary.update(ev.get("changes") or {})
        if t not in ("version", "apply_progress"):
            on_line(ev.get("@message", ln))
    code = run_stream(apply_argv(cfg), cfg, event)
    note = ", ".join(f"{summary.get(k, 0)} {w}" for k, w in
                     (("add", "added"), ("change", "changed"), ("remove", "destroyed")))         if summary else ""
    # A saved plan is single-use; drop it so it can't be applied twice.
    try:
        cfg.plan_file.unlink()
    except OSError:
        pass
    ok = code == 0 and not any(d.severity == "error" for d in diags)
    if not ok and not diags:
        diags.append(Diagnostic("error", "terraform apply failed (see output)"))
    return StageResult("apply", ok, diags, note)
