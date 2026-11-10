"""
Air-Gapped Terraform Porter — a native desktop tool that scans a folder of
Terraform code on an internet-connected machine, works out exactly which
Terraform CLI version and providers it needs, downloads and verifies them for
every target platform, and writes a bundle (filesystem provider mirror + CLI
binaries + mirror-only CLI configs + install scripts + manifest) to carry
across an air gap. `terraform init` on the far side then needs no network and
no change to any .tf file.

Standard library ONLY (tkinter) — no pip install needed. Optional: `gpg` on
PATH (Git for Windows ships one) for signature checks, and `terraform` on PATH
for updating lock files (otherwise the bundled one for this machine is used).
Sibling to Air-Gapped Image Porter / Python Porter; same threading + plan-gate
design.

Run:
    python porter.py                      # GUI
    python porter.py --cli --scan DIR --out BUNDLE [--platforms a,b,c] ...

Organization:
  1. scan_ops / bundle_ops / fetch_ops — scanning, planning, downloading.
  2. JobRunner — serializes steps on a daemon thread; a step is a callable
                 (download/verify) or an (argv, cwd) pair (terraform providers
                 lock). Streams to a queue drained on the UI thread.
  3. App (tk.Tk) — sidebar nav over Scan / Platforms / Build / Files / Preview
                 screens, plus a shared log pane.

Threading rule: network and subprocesses never run on the UI thread.
"""
from __future__ import annotations

import argparse
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import bundle_ops
import fetch_ops
import plan_ops
import scan_ops
import tf_locate


# ---------------------------------------------------------------------------
# JobRunner — runs a list of steps on a background thread. A step is
# (label, action, detail) where action is a callable(emit_line)->int or an
# (argv, cwd) tuple. Stops at the first failure.
# ---------------------------------------------------------------------------
def run_steps_sync(steps, emit) -> str | None:
    """Run steps in order; emit(kind, text). Returns the failed label or None."""
    for label, action, _detail in steps:
        emit("step", label)
        try:
            if callable(action):
                code = action(lambda ln: emit("line", ln))
            else:
                argv, cwd = action
                emit("cmd", f"(in {cwd}) " + " ".join(
                    f'"{a}"' if " " in a else a for a in argv))
                code = bundle_ops.run_argv(argv, cwd, lambda ln: emit("line", ln))
        except Exception as e:                      # noqa: BLE001
            emit("line", f"✗ {e}")
            return label
        if code != 0:
            emit("line", f"✗ exit code {code}")
            return label
        emit("ok", label)
    return None


class JobRunner:
    def __init__(self, out_q: "queue.Queue"):
        self.out_q = out_q
        self.busy = False

    def _emit(self, kind: str, text: str = ""):
        self.out_q.put((kind, text))

    def run_steps(self, steps):
        if self.busy:
            self._emit("line", "⚠ a job is already running — wait for it to finish.")
            return
        self.busy = True
        threading.Thread(target=self._worker, args=(steps,), daemon=True).start()

    def _worker(self, steps):
        failed = None
        try:
            failed = run_steps_sync(steps, self._emit)
        finally:
            self.busy = False
            self._emit("done", failed or "")


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
def _gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext, ttk

    class App(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title("🏗 Air-Gapped Terraform Porter")
            self.geometry("1320x920")
            self._q: queue.Queue = queue.Queue()
            self.runner = JobRunner(self._q)
            self._action_btns: list = []
            self._pending_steps: list = []
            self._after_job = None
            self.scan_result: scan_ops.ScanResult | None = None
            self.platforms = list(bundle_ops.KNOWN_PLATFORMS)
            self._plat_vars: dict[str, tk.BooleanVar] = {}

            self._init_style()
            # Screens above, shared Output log below — a draggable split.
            self.vsplit = ttk.PanedWindow(self, orient="vertical")
            self.vsplit.pack(fill="both", expand=True, padx=8, pady=(8, 8))
            body = ttk.Frame(self.vsplit)
            self.vsplit.add(body, weight=5)
            self.sidebar = ttk.Frame(body, padding=(2, 6), style="Sidebar.TFrame")
            self.sidebar.pack(side="left", fill="y")
            self.nb = ttk.Notebook(body, style="Hidden.TNotebook")
            self.nb.pack(side="left", fill="both", expand=True, padx=(8, 0))
            self.nb.bind("<<NotebookTabChanged>>", self._on_screen_changed)

            self._screens = {}
            self._build_scan_tab()
            self._build_platforms_tab()
            self._build_bundle_tab()
            self._build_files_tab()
            self._build_preview_tab()
            self._build_plan_tab()
            self._build_sidebar_nav()
            self._build_log_pane()

            self._log(f"Host platform: {bundle_ops.host_platform()}")
            self._log_terraform()
            g = fetch_ops.find_gpg()
            self._log(f"gpg: {g}" if g else "⚠ gpg not found — signature checks "
                      "will be skipped (SHA256 checks still run).")
            self.nb.select(self._screens["Scan Project"])
            self.after(120, self._drain)

        def _log_terraform(self):
            """Report the terraform the app will use (PATH of the launching
            terminal first) — found off the UI thread."""
            def work():
                cands = tf_locate.find_all()
                def show():
                    if cands:
                        self._log(f"terraform: {cands[0].label}")
                        for c in cands[1:]:
                            self._log(f"  also found: {c.label}")
                    else:
                        self._log("⚠ terraform not found on PATH — start the app from a "
                                  "terminal where `terraform` works, or use a bundle "
                                  "(its own binary is used for this machine).")
                self.after(0, show)
            threading.Thread(target=work, daemon=True).start()

        # -- styling + nav --------------------------------------------------
        def _init_style(self):
            style = ttk.Style(self)
            try:
                style.theme_use("clam")
            except tk.TclError:
                pass
            style.layout("Hidden.TNotebook.Tab", [])
            style.configure("Sidebar.TFrame", background="#eef1f5")
            style.configure("Nav.TButton", anchor="w", padding=(10, 8), width=20,
                            font=("", 10))
            style.configure("NavHeader.TLabel", background="#eef1f5",
                            foreground="#667085", font=("", 8, "bold"))

        def _build_sidebar_nav(self):
            self._nav_buttons = {}
            ttk.Label(self.sidebar, text="🏗 Terraform Porter",
                      style="NavHeader.TLabel",
                      font=("", 11, "bold")).pack(anchor="w", padx=8, pady=(2, 10))
            layout = [
                ("PREPARE (online side)",
                 ["Scan Project", "Platforms", "Build Bundle", "Preview / Run"]),
                ("RUN (either side)", ["Plan Explorer"]),
                ("FILES", ["Files"]),
            ]
            for header, labels in layout:
                ttk.Label(self.sidebar, text=header, style="NavHeader.TLabel").pack(
                    anchor="w", padx=10, pady=(10, 2))
                for label in labels:
                    b = ttk.Button(self.sidebar, text=label, style="Nav.TButton",
                                   command=lambda fr=self._screens[label]: self.nb.select(fr))
                    b.pack(fill="x", padx=4, pady=1)
                    self._nav_buttons[label] = b

        def _on_screen_changed(self, _e=None):
            sel = self.nb.select()
            for label, b in getattr(self, "_nav_buttons", {}).items():
                b.state(["pressed"] if str(self._screens[label]) == str(sel)
                        else ["!pressed"])
            if sel and str(sel) == str(self._screens.get("Files")):
                self._refresh_files()
            if sel and str(sel) == str(self._screens.get("Plan Explorer")):
                self._plan_view._fill_stacks()

        def _new_screen(self, name):
            tab = ttk.Frame(self.nb, padding=8)
            self.nb.add(tab, text=name)
            self._screens[name] = tab
            return tab

        # -- Scan Project ---------------------------------------------------
        def _build_scan_tab(self):
            tab = self._new_screen("Scan Project")
            ttk.Label(tab, text="Point at a folder of Terraform code. Every *.tf / "
                      "*.tf.json and .terraform.lock.hcl below it is read for "
                      "required_version, required_providers and locked provider "
                      "versions. Nothing is changed.",
                      foreground="#555", wraplength=900, justify="left").pack(anchor="w")
            top = ttk.Frame(tab)
            top.pack(fill="x", pady=(10, 4))
            ttk.Label(top, text="Terraform folder:").pack(side="left")
            self.scan_dir = tk.StringVar(value=str(Path.cwd()))
            ttk.Entry(top, textvariable=self.scan_dir).pack(
                side="left", fill="x", expand=True, padx=4)
            ttk.Button(top, text="Browse…",
                       command=lambda: self._browse_into(self.scan_dir)).pack(side="left")
            self.scan_btn = ttk.Button(top, text="🔍 Scan", command=self._scan)
            self.scan_btn.pack(side="left", padx=4)

            self.core_lbl = ttk.Label(tab, text="Terraform core: (not scanned)",
                                      font=("", 10, "bold"))
            self.core_lbl.pack(anchor="w", pady=(6, 2))

            pf = ttk.LabelFrame(tab, text="Providers", padding=4)
            pf.pack(fill="both", expand=True, pady=4)
            cols = ("provider", "constraints", "locked", "used")
            t = ttk.Treeview(pf, columns=cols, show="headings", height=6)
            for c, text, w in (("provider", "Provider (source address)", 320),
                               ("constraints", "Version constraints", 220),
                               ("locked", "Locked version(s)", 200),
                               ("used", "Used by", 80)):
                t.heading(c, text=text)
                t.column(c, width=w, anchor=("e" if c == "used" else "w"))
            t.pack(fill="both", expand=True)
            self.prov_tree = t

            rf = ttk.LabelFrame(tab, text="Terraform directories", padding=4)
            rf.pack(fill="both", expand=True, pady=4)
            cols = ("dir", "kind", "lock", "providers")
            t = ttk.Treeview(rf, columns=cols, show="headings", height=6)
            for c, text, w in (("dir", "Directory", 300), ("kind", "Kind", 110),
                               ("lock", "Lock file", 90),
                               ("providers", "Providers (local name → source)", 360)):
                t.heading(c, text=text)
                t.column(c, width=w)
            t.pack(fill="both", expand=True)
            self.dir_tree = t

            self.scan_warn = scrolledtext.ScrolledText(tab, height=4, wrap="word",
                                                       font="TkFixedFont")
            self.scan_warn.pack(fill="x", pady=(4, 0))
            self.scan_warn.insert("1.0", "Warnings will appear here.")
            self.scan_warn.config(state="disabled")

        def _scan(self):
            root = Path(self.scan_dir.get().strip() or ".")
            if not root.is_dir():
                messagebox.showwarning("Scan", "Pick a folder that exists.")
                return
            self.scan_btn.config(state="disabled")
            self._log(f"\n🔍 Scanning {root} …")

            def work():
                try:
                    res = scan_ops.scan(root)
                except Exception as e:                       # noqa: BLE001
                    self.after(0, lambda: self._scan_done(None, e))
                    return
                self.after(0, lambda: self._scan_done(res, None))

            threading.Thread(target=work, daemon=True).start()

        def _scan_done(self, res, err):
            self.scan_btn.config(state="normal")
            if err:
                self._log(f"✗ scan failed: {err}")
                return
            self.scan_result = res
            self.prov_tree.delete(*self.prov_tree.get_children(""))
            for addr, req in res.providers.items():
                locked = sorted(set(req.locked.values()))
                self.prov_tree.insert("", "end", values=(
                    addr + ("  (implied)" if req.implied else ""),
                    ", ".join(sorted(req.constraints)) or "(any)",
                    ", ".join(locked) or "— resolve newest match",
                    len(req.used_by)))
            self.dir_tree.delete(*self.dir_tree.get_children(""))
            for m in res.modules:
                try:
                    rel = str(m.path.relative_to(res.root)) or "."
                except ValueError:
                    rel = str(m.path)
                self.dir_tree.insert("", "end", values=(
                    rel, "root" if m.is_root else "child module",
                    "✓" if m.lock_file else "—",
                    ", ".join(f"{k} → {scan_ops.ProviderReq(v).short}"
                              for k, v in sorted(m.providers.items()))))
            core = ", ".join(res.core_constraints) or "(no required_version — newest)"
            self.core_lbl.config(text=f"Terraform core: {core}")
            self.scan_warn.config(state="normal")
            self.scan_warn.delete("1.0", "end")
            self.scan_warn.insert("1.0", "\n".join("⚠ " + w for w in res.warnings)
                                  or "No warnings.")
            self.scan_warn.config(state="disabled")
            self._log(f"  {len(res.modules)} Terraform dir(s), {len(res.roots)} root(s), "
                      f"{len(res.providers)} provider(s); core {core}")
            if not self.bundle_var.get().strip() or self.bundle_var.get() == self._default_bundle:
                self._default_bundle = str(Path.cwd() / f"{res.root.name}-airgap-bundle")
                self.bundle_var.set(self._default_bundle)

        # -- Plan Explorer --------------------------------------------------
        def _build_plan_tab(self):
            import plan_view
            tab = plan_view.PlanExplorer(self.nb, self)
            self.nb.add(tab, text="Plan Explorer")
            self._screens["Plan Explorer"] = tab
            self._plan_view = tab

        # -- Platforms ------------------------------------------------------
        def _build_platforms_tab(self):
            tab = self._new_screen("Platforms")
            ttk.Label(tab, text="Every provider and the Terraform CLI are fetched for "
                      "each ticked platform, and lock files get hashes for all of "
                      "them — so the same bundle runs on Windows workstations and "
                      "Linux/ARM runners in the air gap.",
                      foreground="#555", wraplength=900, justify="left").pack(anchor="w")
            self._plat_box = ttk.LabelFrame(tab, text="Target platforms", padding=8)
            self._plat_box.pack(fill="x", pady=10)
            self._render_platforms()
            add = ttk.Frame(tab)
            add.pack(fill="x")
            ttk.Label(add, text="Add platform (os_arch):").pack(side="left")
            self.new_plat = tk.StringVar()
            ttk.Entry(add, textvariable=self.new_plat, width=20).pack(side="left", padx=4)
            ttk.Button(add, text="+ Add", command=self._add_platform).pack(side="left")
            ttk.Label(add, text="e.g. freebsd_amd64, linux_386. Platforms a provider "
                      "doesn't publish are skipped with a warning.",
                      foreground="#777").pack(side="left", padx=10)

        def _render_platforms(self):
            prev = {p: v.get() for p, v in self._plat_vars.items()}
            for w in self._plat_box.winfo_children():
                w.destroy()
            self._plat_vars = {}
            for i, p in enumerate(self.platforms):
                var = tk.BooleanVar(value=prev.get(p, p in bundle_ops.DEFAULT_PLATFORMS))
                self._plat_vars[p] = var
                note = " (optional extra)" if p.startswith("darwin") else ""
                ttk.Checkbutton(self._plat_box, text=p + note, variable=var).grid(
                    row=i // 3, column=i % 3, sticky="w", padx=8, pady=3)

        def _add_platform(self):
            p = self.new_plat.get().strip().lower()
            if not p or "_" not in p:
                messagebox.showwarning("Platform", "Use os_arch, e.g. linux_386.")
                return
            if p not in self.platforms:
                self.platforms.append(p)
                self._render_platforms()
                self._plat_vars[p].set(True)
            self.new_plat.set("")

        def _selected_platforms(self):
            return [p for p in self.platforms if self._plat_vars[p].get()]

        # -- Build Bundle ---------------------------------------------------
        def _build_bundle_tab(self):
            tab = self._new_screen("Build Bundle")
            ttk.Label(tab, text="Resolves exact versions (lock files win; otherwise "
                      "the newest release matching the constraints) and stages every "
                      "download as a plan. Nothing is downloaded until you Run plan.",
                      foreground="#555", wraplength=900, justify="left").pack(anchor="w")

            out = ttk.Frame(tab)
            out.pack(fill="x", pady=(10, 4))
            ttk.Label(out, text="Bundle folder:").pack(side="left")
            self._default_bundle = str(Path.cwd() / "terraform-airgap-bundle")
            self.bundle_var = tk.StringVar(value=self._default_bundle)
            ttk.Entry(out, textvariable=self.bundle_var).pack(
                side="left", fill="x", expand=True, padx=4)
            ttk.Button(out, text="Browse…",
                       command=lambda: self._browse_into(self.bundle_var)).pack(side="left")

            cli = ttk.LabelFrame(tab, text="Terraform CLI", padding=6)
            cli.pack(fill="x", pady=6)
            self.include_cli = tk.BooleanVar(value=True)
            ttk.Checkbutton(cli, text="include Terraform CLI binaries",
                            variable=self.include_cli).grid(row=0, column=0, sticky="w")
            ttk.Label(cli, text="Version (blank = newest matching required_version):"
                      ).grid(row=0, column=1, sticky="e", padx=(20, 4))
            self.tf_version = tk.StringVar(value="")
            ttk.Entry(cli, textvariable=self.tf_version, width=12).grid(row=0, column=2)

            ver = ttk.LabelFrame(tab, text="Verification", padding=6)
            ver.pack(fill="x", pady=6)
            self.verify_gpg = tk.BooleanVar(value=True)
            ttk.Checkbutton(ver, text="verify GPG signatures on SHA256SUMS "
                            "(HashiCorp key pinned by fingerprint)",
                            variable=self.verify_gpg).pack(anchor="w")
            ttk.Label(ver, text="SHA256 of every zip is always checked against the "
                      "upstream SHA256SUMS (and the registry's own shasum).",
                      foreground="#777").pack(anchor="w")

            lk = ttk.LabelFrame(tab, text="Lock files", padding=6)
            lk.pack(fill="x", pady=6)
            self.update_locks = tk.BooleanVar(value=True)
            ttk.Checkbutton(lk, text="update every root's .terraform.lock.hcl with "
                            "hashes for all chosen platforms (terraform providers lock)",
                            variable=self.update_locks).pack(anchor="w")
            row = ttk.Frame(lk)
            row.pack(anchor="w", pady=(4, 0))
            ttk.Label(row, text="Hash source:").pack(side="left")
            self.lock_source = tk.StringVar(value="bundle")
            ttk.Radiobutton(row, text="the bundle's mirror (offline, fast — h1 hashes)",
                            value="bundle", variable=self.lock_source).pack(side="left", padx=6)
            ttk.Radiobutton(row, text="the registry (re-downloads; adds zh hashes)",
                            value="registry", variable=self.lock_source).pack(side="left", padx=6)

            act = ttk.Frame(tab)
            act.pack(fill="x", pady=(12, 0))
            b = ttk.Button(act, text="📦 Stage bundle build", command=self._stage_bundle)
            b.pack(side="left", padx=4)
            self._action_btns.append(b)
            self.stage_status = ttk.Label(act, text="→ review the downloads in Preview / "
                                          "Run, then run.", foreground="#777")
            self.stage_status.pack(side="left", padx=8)

        def _stage_bundle(self):
            if self.runner.busy:
                messagebox.showinfo("Busy", "A job is already running.")
                return
            res = self.scan_result
            if res is None or str(res.root) != str(Path(self.scan_dir.get()).resolve()):
                messagebox.showwarning("Scan first", "Scan the Terraform folder first "
                                       "(Scan Project screen).")
                return
            plats = self._selected_platforms()
            if not plats:
                messagebox.showwarning("Platforms", "Tick at least one platform.")
                return
            bundle = Path(self.bundle_var.get().strip() or "terraform-airgap-bundle")
            self.stage_status.config(text="Resolving versions…")
            self._log("\n📦 Resolving versions (registry + releases index)…")
            args = dict(platforms=plats, tf_version=self.tf_version.get(),
                        verify_gpg=self.verify_gpg.get(),
                        update_locks=self.update_locks.get(),
                        lock_source=self.lock_source.get(),
                        include_cli=self.include_cli.get())

            def work():
                try:
                    plan = bundle_ops.resolve(res, bundle, **args)
                    steps = bundle_ops.build_steps(plan)
                except Exception as e:                       # noqa: BLE001
                    self.after(0, lambda: self._resolved(None, None, e))
                    return
                self.after(0, lambda: self._resolved(plan, steps, None))

            threading.Thread(target=work, daemon=True).start()

        def _resolved(self, plan, steps, err):
            if err:
                self.stage_status.config(text="Resolve failed — see Output.")
                self._log(f"✗ {err}")
                return
            self.stage_status.config(text="→ review the downloads in Preview / Run, "
                                     "then run.")
            self._log(f"  Terraform CLI: {plan.tf_version or 'not included'} "
                      f"({', '.join(plan.tf_platforms)})")
            if plan.update_locks:
                self._log(f"  lock files: `providers lock` runs with {plan.lock_terraform}")
            for p in plan.providers:
                self._log(f"  {p.address} {p.version} [{p.why}] → {', '.join(p.platforms)}")
            for w in plan.warnings:
                self._log(f"  ⚠ {w}")
            self.files_root_var.set(str(plan.bundle))
            self._stage(steps, after=self._refresh_files)

        # -- Files ----------------------------------------------------------
        def _build_files_tab(self):
            tab = self._new_screen("Files")
            top = ttk.Frame(tab)
            top.pack(fill="x")
            ttk.Label(top, text="Root:").pack(side="left")
            self.files_root_var = tk.StringVar(value=str(Path.cwd()))
            ttk.Entry(top, textvariable=self.files_root_var).pack(
                side="left", fill="x", expand=True, padx=4)
            ttk.Button(top, text="Browse…",
                       command=lambda: (self._browse_into(self.files_root_var),
                                        self._refresh_files())).pack(side="left")
            ttk.Button(top, text="⟳", width=3, command=self._refresh_files).pack(side="left")
            tree = ttk.Treeview(tab, columns=("size", "modified"), height=20)
            tree.heading("#0", text="Name")
            tree.column("#0", width=560)
            tree.heading("size", text="Size")
            tree.column("size", width=110, anchor="e")
            tree.heading("modified", text="Modified")
            tree.column("modified", width=160)
            tree.pack(fill="both", expand=True, pady=6)
            tree.bind("<<TreeviewOpen>>", self._on_file_open)
            self.files_tree = tree
            btns = ttk.Frame(tab)
            btns.pack(fill="x")
            ttk.Button(btns, text="Open in file manager",
                       command=self._open_in_manager).pack(side="left")
            ttk.Label(btns, text="Copy the whole bundle folder to transport media.",
                      foreground="#777").pack(side="left", padx=8)

        def _fmt_size(self, n: float) -> str:
            for unit in ("B", "KB", "MB", "GB"):
                if n < 1024:
                    return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
                n /= 1024
            return f"{n:.1f} TB"

        def _insert_dir(self, parent, path: Path):
            try:
                entries = sorted(path.iterdir(),
                                 key=lambda p: (not p.is_dir(), p.name.lower()))
            except OSError:
                return
            for p in entries:
                try:
                    st = p.stat()
                    mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))
                    size = "" if p.is_dir() else self._fmt_size(st.st_size)
                except OSError:
                    mtime, size = "", ""
                icon = "📁 " if p.is_dir() else "📄 "
                node = self.files_tree.insert(parent, "end", text=icon + p.name,
                                              values=(size, mtime), open=False)
                self.files_tree.item(node, tags=(str(p),))
                if p.is_dir():
                    self.files_tree.insert(node, "end", text="…")

        def _on_file_open(self, _e=None):
            node = self.files_tree.focus()
            tags = self.files_tree.item(node, "tags")
            if not tags:
                return
            kids = self.files_tree.get_children(node)
            if len(kids) == 1 and not self.files_tree.item(kids[0], "tags"):
                self.files_tree.delete(kids[0])
                self._insert_dir(node, Path(tags[0]))

        def _refresh_files(self):
            if not hasattr(self, "files_tree"):
                return
            self.files_tree.delete(*self.files_tree.get_children(""))
            root = Path(self.files_root_var.get().strip() or ".")
            if root.exists():
                self._insert_dir("", root)

        def _open_in_manager(self):
            sel = self.files_tree.focus()
            tags = self.files_tree.item(sel, "tags") if sel else ()
            target = Path(tags[0]) if tags else Path(self.files_root_var.get())
            folder = target if target.is_dir() else target.parent
            try:
                if sys.platform == "win32":
                    os.startfile(folder)  # noqa: S606
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", str(folder)])
                else:
                    subprocess.Popen(["xdg-open", str(folder)])
            except OSError as e:
                messagebox.showerror("Open", str(e))

        # -- Preview / Run --------------------------------------------------
        def _build_preview_tab(self):
            tab = self._new_screen("Preview / Run")
            self._preview_tab = tab
            ttk.Label(tab, text="Every action stages its exact steps here first. "
                      "Nothing runs until you press “Run plan”.",
                      foreground="#555").pack(anchor="w")
            cols = ("#", "action", "command")
            tree = ttk.Treeview(tab, columns=cols, show="headings", height=16)
            tree.heading("#", text="#")
            tree.column("#", width=38, anchor="e", stretch=False)
            tree.heading("action", text="Action")
            tree.column("action", width=330, stretch=False)
            tree.heading("command", text="Source / command")
            tree.column("command", width=640)
            tree.pack(fill="both", expand=True, pady=6)
            self.plan_tree = tree
            bar = ttk.Frame(tab)
            bar.pack(fill="x")
            self.run_plan_btn = ttk.Button(bar, text="▶ Run plan",
                                           command=self._run_plan, state="disabled")
            self.run_plan_btn.pack(side="left")
            ttk.Button(bar, text="Clear plan", command=self._clear_plan).pack(
                side="left", padx=6)
            self.plan_label = ttk.Label(bar, text="No plan staged.", foreground="#777")
            self.plan_label.pack(side="left", padx=12)

        def _render_plan(self):
            self.plan_tree.delete(*self.plan_tree.get_children(""))
            for i, (label, _action, detail) in enumerate(self._pending_steps, 1):
                self.plan_tree.insert("", "end", values=(i, label, detail))
            n = len(self._pending_steps)
            self.plan_label.config(text=(f"{n} step(s) staged — review, then Run plan."
                                         if n else "No plan staged."))
            self.run_plan_btn.config(state=("normal" if n and not self.runner.busy
                                            else "disabled"))

        def _clear_plan(self):
            self._pending_steps = []
            self._pending_after = None
            self._render_plan()

        def _run_plan(self):
            if self.runner.busy or not self._pending_steps:
                return
            steps = self._pending_steps
            self._after_job = getattr(self, "_pending_after", None)
            self._pending_steps = []
            self._pending_after = None
            self._render_plan()
            self._set_busy(True)
            self.runner.run_steps(steps)

        # -- log pane + plumbing --------------------------------------------
        def _build_log_pane(self):
            f = ttk.LabelFrame(self.vsplit, text="Output  (drag the divider to resize)",
                               padding=4)
            self.vsplit.add(f, weight=1)
            self.log = scrolledtext.ScrolledText(f, height=7, wrap="word",
                                                 state="disabled", font=("Consolas", 9))
            self.log.pack(fill="both", expand=True)
            bottom = ttk.Frame(f)
            bottom.pack(fill="x", pady=(4, 0))
            self.status = ttk.Label(bottom, text="Ready.")
            self.status.pack(side="left")
            ttk.Button(bottom, text="Clear", command=self._clear_log).pack(side="right")

        def _clear_log(self):
            self.log.config(state="normal")
            self.log.delete("1.0", "end")
            self.log.config(state="disabled")

        def _log(self, text: str):
            self.log.config(state="normal")
            self.log.insert("end", text + "\n")
            self.log.see("end")
            self.log.config(state="disabled")

        def _set_busy(self, busy: bool):
            state = "disabled" if busy else "normal"
            for b in self._action_btns:
                b.config(state=state)
            self.run_plan_btn.config(
                state=("disabled" if busy or not self._pending_steps else "normal"))
            self.status.config(text="Working…" if busy else "Ready.")

        def _stage(self, steps, after=None):
            if not steps:
                return
            self._pending_steps = steps
            self._pending_after = after
            self._render_plan()
            self.nb.select(self._preview_tab)
            self._log(f"Staged {len(steps)} step(s) → review in Preview / Run.")

        def _drain(self):
            try:
                while True:
                    kind, text = self._q.get_nowait()
                    if kind == "step":
                        self._log(f"\n▶ {text}")
                    elif kind == "cmd":
                        self._log(f"  $ {text}")
                    elif kind == "line":
                        self._log(f"    {text}")
                    elif kind == "ok":
                        self._log(f"  ✓ {text}")
                    elif kind == "done":
                        self._set_busy(False)
                        self._log(f"\n✗ stopped at: {text}" if text
                                  else "\n✓ all steps completed.")
                        cb, self._after_job = self._after_job, None
                        if cb:
                            cb()
            except queue.Empty:
                pass
            self.after(120, self._drain)

        def _browse_into(self, var):
            d = filedialog.askdirectory(title="Choose folder")
            if d:
                var.set(d)

    App().mainloop()


def _capture(argv):
    kw = {}
    if sys.platform == "win32":
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        p = subprocess.run(argv, capture_output=True, text=True, **kw)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except OSError as e:
        return 1, str(e)


# ---------------------------------------------------------------------------
# Headless mode — same scan / plan / steps, printed to the console.
# ---------------------------------------------------------------------------
def _cli(argv) -> int:
    ap = argparse.ArgumentParser(prog="porter.py --cli",
                                 description="Build a Terraform air-gap bundle.")
    ap.add_argument("--scan", required=True, help="folder of Terraform code")
    ap.add_argument("--out", required=True, help="bundle folder to write")
    ap.add_argument("--platforms", default=",".join(bundle_ops.DEFAULT_PLATFORMS))
    ap.add_argument("--tf-version", default="", help="pin the CLI version")
    ap.add_argument("--no-cli", action="store_true", help="skip Terraform binaries")
    ap.add_argument("--no-gpg", action="store_true", help="skip signature checks")
    ap.add_argument("--no-lock", action="store_true", help="don't touch lock files")
    ap.add_argument("--lock-source", choices=["bundle", "registry"], default="bundle")
    ap.add_argument("--terraform", default="",
                    help="terraform binary for `providers lock` (default: the one on PATH)")
    ap.add_argument("--plan-only", action="store_true", help="print the plan and exit")
    a = ap.parse_args(argv)

    res = scan_ops.scan(Path(a.scan))
    print(f"scanned {res.root}: {len(res.modules)} dir(s), {len(res.roots)} root(s), "
          f"core {', '.join(res.core_constraints) or '(any)'}")
    for addr, req in res.providers.items():
        print(f"  {addr}  constraints={sorted(req.constraints)}  "
              f"locked={sorted(set(req.locked.values()))}")
    plan = bundle_ops.resolve(res, Path(a.out), a.platforms.split(","),
                              tf_version=a.tf_version, verify_gpg=not a.no_gpg,
                              update_locks=not a.no_lock, lock_source=a.lock_source,
                              terraform=a.terraform,
                              include_cli=not a.no_cli)
    for w in plan.warnings:
        print(f"  ⚠ {w}")
    steps = bundle_ops.build_steps(plan)
    for i, (label, _a, detail) in enumerate(steps, 1):
        print(f"  {i:3d}. {label:<48} {detail}")
    if a.plan_only:
        return 0

    def emit(kind, text):
        prefix = {"step": "\n▶ ", "cmd": "  $ ", "line": "    ", "ok": "  ✓ "}[kind]
        if kind == "line" and text.strip().endswith("MB") and "%" in text:
            return                                    # skip progress spam
        print(prefix + text, flush=True)

    failed = run_steps_sync(steps, emit)
    print(f"\n✗ stopped at: {failed}" if failed else "\n✓ all steps completed.")
    return 1 if failed else 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--cli":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
        sys.exit(_cli(sys.argv[2:]))
    _gui()
