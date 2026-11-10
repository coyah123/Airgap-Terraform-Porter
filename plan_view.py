"""
plan_view — the Plan Explorer screen: a GUI over terraform init / validate /
plan that shows whether a stack passes and draws what the plan will do as a
tree, much like Argo CD's UI draws an application's resources:

    stack → modules → resources (count / for_each instances grouped)

Boxes are coloured by planned action (create / update / replace / destroy /
read / no change). Single-click a box for its detail panel: address, action
and Terraform's reason, the resources it references, and an attribute-level
before → after diff ("known after apply" and sensitive values marked). A
Map / List toggle swaps the drawn map for an indented table.

init / validate / plan are read-only. Apply is a separate, guarded step: it
applies exactly the saved plan shown, only after a typed confirmation, and is
refused for temporary no-state plans of stacks with a real backend or when
files changed after the plan. Boxes tick ✓ / ✗ live while it runs.
"""
from __future__ import annotations

import json
import shutil
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import bundle_ops
import plan_ops
import tf_locate
from plan_ops import ACTIONS

# Box fills per action (dark canvas, same palette family as the Argo map).
FILL = {"create": "#1e3a1e", "update": "#5a4a1e", "replace": "#5e3417",
        "delete": "#5a1e1e", "read": "#1e2e4a", "no-op": "#2d2d2d",
        "forget": "#3a2d4a"}
ROW_BG = {"create": "#d9f2d9", "update": "#fff1c2", "replace": "#ffd9b8",
          "delete": "#f8d0d0", "read": "#d6e4f7", "forget": "#e6dcf2"}
BW, BH, HGAP, VGAP = 210, 48, 70, 14


class PlanExplorer(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master, padding=8)
        self.app = app                      # for _log / scan_result / bundle_var
        self.busy = False
        self.plan: dict | None = None
        self.tree_root: plan_ops.Node | None = None
        self.node_by_tag: dict[str, plan_ops.Node] = {}
        self.node_by_iid: dict[str, plan_ops.Node] = {}
        self.diags: list[plan_ops.Diagnostic] = []
        self._buttons: list[ttk.Button] = []
        self.run_cfg: plan_ops.RunConfig | None = None   # config the plan came from
        self.planned_fp: dict | None = None              # file mtimes at plan time
        self.apply_state: dict[str, str] = {}            # address -> applying/done/error
        self._build_form()
        self._build_status()
        self._build_body()

    # -- form -------------------------------------------------------------------
    def _build_form(self):
        ttk.Label(self, text="Run terraform init → validate → plan on one stack and "
                  "see what it would do. Plans only read the target account. "
                  "⚠ Apply changes real infrastructure: it applies exactly the plan "
                  "shown, after you confirm by typing the stack name.",
                  foreground="#555", wraplength=1000, justify="left").pack(anchor="w")
        f = ttk.Frame(self)
        f.pack(fill="x", pady=(6, 2))
        f.columnconfigure(1, weight=1)

        ttk.Label(f, text="Stack:").grid(row=0, column=0, sticky="e")
        self.stack_var = tk.StringVar()
        self.stack_box = ttk.Combobox(f, textvariable=self.stack_var)
        self.stack_box.grid(row=0, column=1, sticky="ew", padx=4)
        self.stack_box.bind("<<ComboboxSelected>>", lambda _e: self._stack_changed())
        self.stack_box.bind("<Button-1>", lambda _e: self._fill_stacks())
        ttk.Button(f, text="Browse…", command=self._browse_stack).grid(row=0, column=2)

        ttk.Label(f, text="Var files:").grid(row=1, column=0, sticky="e")
        self.vars_var = tk.StringVar()
        ttk.Entry(f, textvariable=self.vars_var).grid(row=1, column=1, sticky="ew", padx=4)
        ttk.Button(f, text="Add…", command=self._add_varfile).grid(row=1, column=2)

        ttk.Label(f, text="AWS profile:").grid(row=3, column=0, sticky="e")
        self.profile_var = tk.StringVar(value="")
        prof = ttk.Combobox(f, textvariable=self.profile_var, width=30,
                            values=[""] + plan_ops.aws_profiles())
        prof.grid(row=3, column=1, sticky="w", padx=4)
        ttk.Label(f, text="blank = environment's default credentials. A plan reads "
                  "the account; it never changes it.", foreground="#777").grid(
            row=3, column=1, sticky="w", padx=(250, 0))

        # Terraform binary: defaults to the one on the launching terminal's PATH
        # (tf_locate); every other one found is in the dropdown. Status below.
        ttk.Label(f, text="Terraform:").grid(row=2, column=0, sticky="e")
        self.tf_var = tk.StringVar(value=shutil.which("terraform") or "")
        self.tf_box = ttk.Combobox(f, textvariable=self.tf_var)
        self.tf_box.grid(row=2, column=1, sticky="ew", padx=4)
        self.tf_box.bind("<<ComboboxSelected>>", lambda _e: self._update_tf_status())
        self.tf_box.bind("<FocusOut>", lambda _e: self._update_tf_status())
        tfb = ttk.Frame(f)
        tfb.grid(row=2, column=2, sticky="w")
        ttk.Button(tfb, text="Browse…", command=self._browse_tf).pack(side="left")
        ttk.Button(tfb, text="⟳", width=3, command=self._detect_tf).pack(side="left")
        self.tf_status = ttk.Label(f, text="Looking for terraform…", foreground="#777")
        self.tf_status.grid(row=4, column=1, sticky="w", padx=4)
        self._tf_cands: dict[str, tf_locate.Candidate] = {}
        self.after(200, self._detect_tf)

        opts = ttk.Frame(self)
        opts.pack(fill="x", pady=(2, 2))
        self.use_bundle = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="providers from bundle (offline):",
                        variable=self.use_bundle,
                        command=self._bundle_toggled).pack(side="left")
        self.bundle_dir = tk.StringVar()
        ttk.Entry(opts, textvariable=self.bundle_dir, width=46).pack(side="left", padx=4)
        ttk.Button(opts, text="…", width=3, command=self._browse_bundle).pack(side="left")
        self.local_backend = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="local backend, no state (temporary override)",
                        variable=self.local_backend,
                        command=self._backend_toggled).pack(side="left", padx=(16, 4))
        ttk.Label(opts, text="else -backend-config:").pack(side="left")
        self.backend_cfg = tk.StringVar()
        self.backend_entry = ttk.Entry(opts, textvariable=self.backend_cfg, width=30,
                                       state="disabled")
        self.backend_entry.pack(side="left", padx=4)

        act = ttk.Frame(self)
        act.pack(fill="x", pady=(6, 2))
        for text, cmd in (("▶ Init + Validate + Plan", self._run_all),
                          ("✓ Validate only", self._run_validate),
                          ("📂 Open plan file…", self._open_plan)):
            b = ttk.Button(act, text=text, command=cmd)
            b.pack(side="left", padx=(0, 6))
            self._buttons.append(b)
        self.save_btn = ttk.Button(act, text="💾 Save plan JSON…", command=self._save_plan,
                                   state="disabled")
        self.save_btn.pack(side="left")
        ttk.Separator(act, orient="vertical").pack(side="left", fill="y", padx=10)
        ttk.Style(self).configure("Apply.TButton", foreground="#8a1010",
                                  font=("", 9, "bold"))
        self.apply_btn = ttk.Button(act, text="⚠ Apply this plan…", command=self._apply,
                                    state="disabled", style="Apply.TButton")
        self.apply_btn.pack(side="left")
        self.apply_why = ttk.Label(act, text="", foreground="#8a5a00")
        self.apply_why.pack(side="left", padx=6)
        self.cmd_lbl = ttk.Label(act, text="", foreground="#777")
        self.cmd_lbl.pack(side="left", padx=10)

    def _build_status(self):
        s = ttk.Frame(self)
        s.pack(fill="x", pady=(4, 2))
        self.badges = {}
        for name in ("init", "validate", "plan", "apply"):
            lbl = tk.Label(s, text=f"○ {name}", padx=8, pady=2, bg="#e4e7ec",
                           fg="#333", font=("", 9, "bold"))
            lbl.pack(side="left", padx=(0, 4))
            self.badges[name] = lbl
        self.summary = ttk.Label(s, text="No plan yet.", font=("", 10, "bold"))
        self.summary.pack(side="left", padx=10)

    def _set_badge(self, name, state, note=""):
        colors = {"run": ("#cfe0fc", "#123", "◌"), "ok": ("#c8ecc8", "#143d14", "✓"),
                  "fail": ("#f6c4c4", "#5a0f0f", "✗"), "idle": ("#e4e7ec", "#333", "○"),
                  "skip": ("#e4e7ec", "#888", "–")}
        bg, fg, sym = colors[state]
        self.badges[name].config(text=f"{sym} {name}" + (f": {note}" if note else ""),
                                 bg=bg, fg=fg)

    # -- body: map/list + side panel ---------------------------------------------
    def _build_body(self):
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(4, 2))
        ttk.Label(bar, text="View:").pack(side="left")
        self.view_mode = tk.StringVar(value="Map")
        for v in ("Map", "List"):
            ttk.Radiobutton(bar, text=v, value=v, variable=self.view_mode,
                            command=self._render).pack(side="left")
        self.show_noop = tk.BooleanVar(value=True)
        self.show_data = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="unchanged", variable=self.show_noop,
                        command=self._rebuild).pack(side="left", padx=(14, 0))
        ttk.Checkbutton(bar, text="data sources", variable=self.show_data,
                        command=self._rebuild).pack(side="left")
        legend = ttk.Frame(bar)
        legend.pack(side="right")
        for key in ("create", "update", "replace", "delete", "read", "no-op"):
            sym, label, _ = ACTIONS[key]
            tk.Label(legend, text=f" {sym} {label} ", bg=FILL[key], fg="white",
                     font=("", 8, "bold")).pack(side="left", padx=1)

        panes = ttk.PanedWindow(self, orient="horizontal")
        panes.pack(fill="both", expand=True, pady=(2, 0))
        left = ttk.Frame(panes)
        panes.add(left, weight=3)

        # Map
        self.map_wrap = ttk.Frame(left)
        cv = tk.Canvas(self.map_wrap, background="#1e1e1e", highlightthickness=0)
        vs = ttk.Scrollbar(self.map_wrap, orient="vertical", command=cv.yview)
        hs = ttk.Scrollbar(self.map_wrap, orient="horizontal", command=cv.xview)
        cv.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        hs.pack(side="bottom", fill="x")
        vs.pack(side="right", fill="y")
        cv.pack(side="left", fill="both", expand=True)
        bind_wheel(cv)
        self.canvas = cv

        # List
        self.list_wrap = ttk.Frame(left)
        cols = ("action", "address", "reason")
        lv = ttk.Treeview(self.list_wrap, columns=cols, show="tree headings")
        lvs = ttk.Scrollbar(self.list_wrap, orient="vertical", command=lv.yview)
        lv.configure(yscrollcommand=lvs.set)
        lv.heading("#0", text="resource")
        lv.column("#0", width=280)
        for c, w in zip(cols, (90, 330, 180)):
            lv.heading(c, text=c)
            lv.column(c, width=w)
        for key, bg in ROW_BG.items():
            lv.tag_configure(key, background=bg)
        lv.pack(side="left", fill="both", expand=True)
        lvs.pack(side="right", fill="y")
        lv.bind("<<TreeviewSelect>>", self._list_selected)
        self.listview = lv

        # Side panel: details + problems
        right = ttk.Frame(panes)
        panes.add(right, weight=2)
        self.side = ttk.Notebook(right)
        self.side.pack(fill="both", expand=True)
        det = ttk.Frame(self.side, padding=4)
        self.side.add(det, text="Details")
        self.det_title = ttk.Label(det, text="Click a box to inspect it.",
                                   font=("", 10, "bold"), wraplength=420)
        self.det_title.pack(anchor="w")
        self.det_info = ttk.Label(det, text="", foreground="#555", wraplength=420,
                                  justify="left")
        self.det_info.pack(anchor="w", pady=(2, 4))
        self.only_changed = tk.BooleanVar(value=True)
        ttk.Checkbutton(det, text="only changed attributes", variable=self.only_changed,
                        command=lambda: self._show_node(self._current)).pack(anchor="w")
        at = ttk.Treeview(det, columns=("before", "after"), show="tree headings", height=14)
        at.heading("#0", text="attribute")
        at.column("#0", width=170)
        at.heading("before", text="before")
        at.column("before", width=130)
        at.heading("after", text="after")
        at.column("after", width=150)
        at.tag_configure("chg", background="#fff1c2")
        at.tag_configure("unk", foreground="#7a5a00")
        at.pack(fill="both", expand=True)
        self.attrs = at
        self._current = None

        prob = ttk.Frame(self.side, padding=4)
        self.side.add(prob, text="Problems")
        self.prob_tab = prob
        pv = ttk.Treeview(prob, columns=("where", "summary"), show="headings", height=8)
        pv.heading("where", text="where")
        pv.column("where", width=140)
        pv.heading("summary", text="summary")
        pv.column("summary", width=320)
        pv.tag_configure("error", background="#f8d0d0")
        pv.tag_configure("warning", background="#fff1c2")
        pv.pack(fill="both", expand=True)
        pv.bind("<<TreeviewSelect>>", self._diag_selected)
        self.probs = pv
        self.prob_detail = tk.Text(prob, height=8, wrap="word", font="TkFixedFont")
        self.prob_detail.pack(fill="both", expand=False, pady=(4, 0))
        self._render()

    # -- form helpers ----------------------------------------------------------
    def _fill_stacks(self):
        res = getattr(self.app, "scan_result", None)
        if res:
            self.stack_box["values"] = [str(r.path) for r in res.roots]
            if not self.stack_var.get() and res.roots:
                self.stack_var.set(str(res.roots[0].path))
                self._stack_changed()

    def _stack_changed(self):
        """Prefill: lab/example tfvars, a bundle next to the stack."""
        st = Path(self.stack_var.get())
        if not st.is_dir():
            return
        self.after(50, self._update_tf_status)
        if not self.vars_var.get():
            envs = st / "environments"
            cands = sorted(envs.glob("*.tfvars")) if envs.is_dir() else []
            if cands:
                self.vars_var.set(str(cands[0].relative_to(st)))
        if not self.bundle_dir.get():
            found = find_bundle(st, getattr(self.app, "bundle_var", None))
            if found:
                self.bundle_dir.set(str(found))

    def _browse_stack(self):
        d = filedialog.askdirectory(title="Terraform stack (root module)")
        if d:
            self.stack_var.set(d)
            self.vars_var.set("")
            self._stack_changed()

    def _add_varfile(self):
        st = Path(self.stack_var.get() or ".")
        files = filedialog.askopenfilenames(
            title="Var files", initialdir=str(st if st.is_dir() else Path.cwd()),
            filetypes=[("tfvars", "*.tfvars *.tfvars.json"), ("All", "*.*")])
        if files:
            cur = [v for v in self.vars_var.get().split(";") if v.strip()]
            for f in files:
                p = Path(f)
                try:
                    p = p.relative_to(st)
                except ValueError:
                    pass
                cur.append(str(p))
            self.vars_var.set(";".join(cur))

    def _browse_tf(self):
        f = filedialog.askopenfilename(title="terraform binary")
        if f:
            self.tf_var.set(f)
            self._update_tf_status()

    def _bundle_path(self) -> Path | None:
        b = self.bundle_dir.get().strip() if hasattr(self, "bundle_dir") else ""
        return Path(b) if b else None

    def _detect_tf(self):
        """Find every terraform (PATH of the launching terminal first) off the UI
        thread, fill the dropdown, and default to the best one."""
        self.tf_status.config(text="Looking for terraform…", foreground="#777")
        bundle = self._bundle_path()

        def work():
            cands = tf_locate.find_all(bundle, bundle_ops.host_platform())
            self.after(0, lambda: self._tf_detected(cands))
        threading.Thread(target=work, daemon=True).start()

    def _tf_detected(self, cands):
        self._tf_cands = {c.path: c for c in cands}
        self.tf_box["values"] = [c.path for c in cands]
        cur = self.tf_var.get().strip()
        if not cur or (cur not in self._tf_cands and not Path(cur).is_file()):
            self.tf_var.set(cands[0].path if cands else "")
        self._update_tf_status()

    def _update_tf_status(self):
        """Show which terraform will run, where it came from, and whether its
        version satisfies the stack's required_version."""
        path = self.tf_var.get().strip()
        if not path:
            self.tf_status.config(
                text="⚠ No terraform found on PATH (or in a bundle). Install it, start "
                     "the app from a terminal where `terraform` works, Browse to one, or "
                     "tick 'providers from bundle'.", foreground="#a00")
            return
        c = self._tf_cands.get(path) or tf_locate.describe(path)
        if not c.version:
            self.tf_status.config(text=f"⚠ {path} — can't run it (not a terraform binary, "
                                       "or built for another OS/CPU)", foreground="#a00")
            return
        text, color = c.label.split(" — ", 2)[0] + " — " + {
            "PATH": "from your terminal's PATH", "shell": "from your login shell's PATH",
            "common": "found in a common install folder", "bundle": "from the bundle",
            "chosen": "chosen by you"}.get(c.source, c.source), "#163"
        st = Path(self.stack_var.get().strip())
        if st.is_dir():
            try:
                import scan_ops
                from tf_versions import allows_all
                cons = sorted({v for m in scan_ops.scan(st).modules
                               for v in m.required_versions})
                if cons and not allows_all(cons, c.version):
                    text += f"   ⚠ stack requires {', '.join(cons)}"
                    color = "#a00"
                elif cons:
                    text += f"   ✓ meets {', '.join(cons)}"
            except Exception:                        # noqa: BLE001 - status only
                pass
        self.tf_status.config(text=text, foreground=color)

    def _browse_bundle(self):
        d = filedialog.askdirectory(title="Bundle folder (contains providers/)")
        if d:
            self.bundle_dir.set(d)
            self.use_bundle.set(True)
            self._bundle_toggled()

    def _bundle_toggled(self):
        # Re-detect so the bundle's own terraform joins the list. PATH still wins
        # as the default; the bundle's binary is used only if nothing else exists.
        if self.use_bundle.get():
            self._detect_tf()

    def _backend_toggled(self):
        self.backend_entry.config(state="disabled" if self.local_backend.get() else "normal")

    def _config(self) -> plan_ops.RunConfig | None:
        st = Path(self.stack_var.get().strip())
        if not (st.is_dir() and any(st.glob("*.tf"))):
            messagebox.showwarning("Plan", "Pick a stack folder containing .tf files "
                                   "(Browse…, or Scan Project first and pick from the list).")
            return None
        tf = self.tf_var.get().strip()
        if not tf:
            best = tf_locate.default_terraform(self._bundle_path(),
                                               bundle_ops.host_platform())
            tf = best.path if best else ""
            self.tf_var.set(tf)
        if not tf:
            messagebox.showwarning("Plan", "No terraform found on PATH or in a bundle — "
                                   "install it, Browse to one, or use a bundle.")
            return None
        bundle = None
        if self.use_bundle.get():
            bundle = Path(self.bundle_dir.get().strip())
            if not (bundle / "providers").is_dir():
                messagebox.showwarning("Plan", f"No providers/ folder in {bundle}")
                return None
        varfiles = [v.strip() for v in self.vars_var.get().split(";") if v.strip()]
        for v in varfiles:
            if not (st / v).is_file() and not Path(v).is_file():
                messagebox.showwarning("Plan", f"Var file not found: {v}")
                return None
        return plan_ops.RunConfig(st.resolve(), tf, bundle, self.local_backend.get(),
                                  self.backend_cfg.get().strip(), varfiles,
                                  self.profile_var.get().strip())

    # -- running ----------------------------------------------------------------
    def _log(self, text):
        self.app._q.put(("line", text))

    def _busy(self, on: bool):
        self.busy = on
        for b in self._buttons:
            b.config(state="disabled" if on else "normal")
        if on:
            self.apply_btn.config(state="disabled")

    def _run_all(self):
        self._start(plan=True)

    def _run_validate(self):
        self._start(plan=False)

    def _start(self, plan: bool):
        if self.busy:
            return
        cfg = self._config()
        if not cfg:
            return
        self._busy(True)
        for n in ("init", "validate", "plan", "apply"):
            self._set_badge(n, "idle")
        self.run_cfg = None
        self.planned_fp = None
        self.apply_state = {}
        if not plan:
            self._set_badge("plan", "skip")
        self.summary.config(text="Running…", foreground="")
        # Drop the previous result so a failure never shows a stale map.
        self.plan = None
        self.tree_root = None
        self.save_btn.config(state="disabled")
        self._show_node(None)
        self.det_title.config(text="Click a box to inspect it.")
        self.det_info.config(text="")
        self._render()
        self.diags = []
        self._render_problems()
        self.cmd_lbl.config(text=plan_ops.display(plan_ops.plan_argv(cfg) if plan
                                                  else plan_ops.validate_argv(cfg))[:160])
        self.app._q.put(("step", f"Plan Explorer: {cfg.stack.name}"))
        threading.Thread(target=self._work, args=(cfg, plan), daemon=True).start()

    def _ui(self, fn, *a):
        self.after(0, lambda: fn(*a))

    def _work(self, cfg: plan_ops.RunConfig, do_plan: bool):
        diags: list = []
        plan = None
        try:
            with plan_ops.LocalBackend(cfg):
                if cfg.local_backend:
                    self._log(f"(temporary {plan_ops.OVERRIDE_NAME} written; "
                              f"TF_DATA_DIR={cfg.data_dir})")
                self._ui(self._set_badge, "init", "run")
                self.app._q.put(("cmd", plan_ops.display(plan_ops.init_argv(cfg))))
                r = plan_ops.run_init(cfg, self._log)
                diags += r.diagnostics
                self._ui(self._set_badge, "init", "ok" if r.ok else "fail")
                if not r.ok:
                    return self._ui(self._done, diags, None, "init failed")
                self._ui(self._set_badge, "validate", "run")
                r = plan_ops.run_validate(cfg, self._log)
                diags += r.diagnostics
                self._ui(self._set_badge, "validate", "ok" if r.ok else "fail",
                         r.note if r.ok and not r.note.startswith("0 ") else "")
                if not r.ok:
                    return self._ui(self._done, diags, None, "configuration is not valid")
                if not do_plan:
                    return self._ui(self._done, diags, None, "valid ✓ (plan not run)")
                self._ui(self._set_badge, "plan", "run")
                self.app._q.put(("cmd", plan_ops.display(plan_ops.plan_argv(cfg))))
                fp = plan_ops.input_fingerprint(cfg)       # taken before planning
                r, plan = plan_ops.run_plan(cfg, self._log)
                if r.ok:
                    self.run_cfg, self.planned_fp = cfg, fp
                diags += r.diagnostics
                self._ui(self._set_badge, "plan", "ok" if r.ok else "fail")
                return self._ui(self._done, diags, plan,
                                None if r.ok else "plan failed")
        except Exception as e:                       # noqa: BLE001
            diags.append(plan_ops.Diagnostic("error", str(e)))
            self._ui(self._done, diags, None, str(e))
        finally:
            if cfg.local_backend and (cfg.stack / plan_ops.OVERRIDE_NAME).exists():
                self._log("⚠ temporary override still present — remove it by hand")

    def _done(self, diags, plan, failure):
        self._busy(False)
        self.diags = diags
        self._render_problems()
        if plan is not None:
            self._load(plan, Path(self.stack_var.get()).name)
        if failure:
            self.summary.config(text=("✗ " if "✓" not in failure else "") + failure,
                                foreground="#a00" if "✓" not in failure else "#163")
            if any(d.severity == "error" for d in diags):
                self.side.select(self.prob_tab)
        ok = not failure or "✓" in failure
        self.app._q.put(("ok" if ok else "line",
                         "Plan Explorer finished" if ok else f"✗ Plan Explorer: {failure}"))
        self._refresh_apply()

    def _load(self, plan: dict, title: str):
        self.plan = plan
        self.save_btn.config(state="normal")
        line = plan_ops.summary_line(plan)
        ok = not plan.get("errored")
        self.summary.config(text=("✓ " if ok else "✗ ") + line,
                            foreground="#163" if ok else "#a00")
        self._title = title
        self._rebuild()
        self._refresh_apply()

    # -- apply ------------------------------------------------------------------
    def _refresh_apply(self):
        """Enable Apply only when every guard passes; otherwise say why."""
        if self.run_cfg is None:
            why = ["view-only (opened from a file)"] if self.plan is not None else []
        else:
            why = plan_ops.apply_blockers(self.run_cfg, self.plan, self.planned_fp)
        ok = self.run_cfg is not None and not why and not self.busy
        self.apply_btn.config(state="normal" if ok else "disabled")
        self.apply_why.config(text="" if ok or not why else
                              "apply unavailable: " + _clip(why[0], 90))

    def _apply(self):
        cfg = self.run_cfg
        why = plan_ops.apply_blockers(cfg, self.plan, self.planned_fp) if cfg else \
            ["No plan from this session."]
        if why:
            messagebox.showwarning("Apply", "\n\n".join(why))
            self._refresh_apply()
            return
        if not ConfirmApply(self, cfg, self.plan).result:
            self._log("apply cancelled at confirmation")
            return
        self.start_apply(cfg)

    def start_apply(self, cfg):
        self._busy(True)
        self.apply_why.config(text="")
        self.apply_state = {}
        self._set_badge("apply", "run")
        self.summary.config(text="Applying…", foreground="#8a1010")
        self.app._q.put(("step", f"APPLY {cfg.stack.name}"))
        self.app._q.put(("cmd", plan_ops.display(plan_ops.apply_argv(cfg))))
        threading.Thread(target=self._apply_work, args=(cfg,), daemon=True).start()

    def _apply_work(self, cfg):
        def on_res(addr, state):
            self._ui(self._mark, addr, state)
        try:
            with plan_ops.LocalBackend(cfg):
                r = plan_ops.run_apply(cfg, self._log, on_res)
        except Exception as e:                       # noqa: BLE001
            r = plan_ops.StageResult("apply", False, [plan_ops.Diagnostic("error", str(e))])
        self._ui(self._apply_done, r)

    def _mark(self, addr, state):
        self.apply_state[addr] = state
        self._render(keep_view=True)

    def _apply_done(self, r):
        self._busy(False)
        self.run_cfg = None                  # the plan file is consumed
        self.diags = self.diags + r.diagnostics
        self._render_problems()
        self._set_badge("apply", "ok" if r.ok else "fail", r.note)
        if r.ok:
            self.summary.config(text=f"✓ Apply complete: {r.note}. Re-plan to confirm "
                                     "nothing is left to change.", foreground="#163")
            self.app._q.put(("ok", f"apply complete: {r.note}"))
        else:
            self.summary.config(text="✗ Apply failed — see Problems. Some changes may "
                                     "have been made; re-plan to see where it stands.",
                                foreground="#a00")
            self.side.select(self.prob_tab)
            self.app._q.put(("line", "✗ apply failed"))
        self.apply_btn.config(state="disabled")
        self.apply_why.config(text="plan used — re-plan to apply again")
        self._render(keep_view=True)

    def _open_plan(self):
        f = filedialog.askopenfilename(
            title="Open a plan (binary .tfplan or `terraform show -json` output)",
            filetypes=[("Plans", "*.tfplan *.json *.plan"), ("All", "*.*")])
        if not f:
            return
        try:
            best = tf_locate.default_terraform()
            plan = plan_ops.load_plan_file(Path(f), self.tf_var.get().strip()
                                           or (best.path if best else "terraform"))
        except Exception as e:                       # noqa: BLE001
            messagebox.showerror("Open plan", str(e))
            return
        for n in ("init", "validate", "plan", "apply"):
            self._set_badge(n, "skip")
        self._set_badge("plan", "ok", Path(f).name)
        self.run_cfg = None                  # opened files are view-only
        self.planned_fp = None
        self.apply_state = {}
        self.diags = []
        self._render_problems()
        self._load(plan, Path(f).stem)

    def _save_plan(self):
        if not self.plan:
            return
        f = filedialog.asksaveasfilename(defaultextension=".json",
                                         initialfile=f"{getattr(self, '_title', 'plan')}"
                                                     ".plan.json",
                                         filetypes=[("JSON", "*.json")])
        if f:
            Path(f).write_text(json.dumps(self.plan, indent=1), encoding="utf-8")
            self._log(f"saved plan JSON → {f}")

    # -- rendering --------------------------------------------------------------
    def _rebuild(self):
        if self.plan is not None:
            self.tree_root = plan_ops.build_tree(self.plan, getattr(self, "_title", "plan"),
                                                 self.show_noop.get(), self.show_data.get())
        self._render()

    def _render(self, keep_view=False):
        self.map_wrap.pack_forget()
        self.list_wrap.pack_forget()
        if self.view_mode.get() == "List":
            self.list_wrap.pack(fill="both", expand=True)
            self._render_list()
        else:
            self.map_wrap.pack(fill="both", expand=True)
            self._render_map(keep_view)

    def _render_map(self, keep_view=False):
        cv = self.canvas
        view = (cv.xview()[0], cv.yview()[0])
        cv.delete("all")
        self.node_by_tag.clear()
        root = self.tree_root
        if root is None:
            cv.create_text(20, 20, anchor="nw", fill="#aaa", font=("", 10),
                           text="Run a plan (▶ Init + Validate + Plan) or open a "
                                "saved one to see what it will do.")
            return
        _layout(root, 0, 0)
        self._draw(root)
        bbox = cv.bbox("all")
        if bbox:
            cv.configure(scrollregion=(0, 0, bbox[2] + 20, bbox[3] + 20))
        cv.xview_moveto(view[0] if keep_view else 0)
        cv.yview_moveto(view[1] if keep_view else 0)

    def _draw(self, node, n=[0]):
        cv = self.canvas
        for c in node.children:
            x1, y1 = node.x + BW, node.y + BH / 2
            x2, y2 = c.x, c.y + BH / 2
            mid = (x1 + x2) / 2
            cv.create_line(x1, y1, mid, y1, mid, y2, x2, y2, fill="#666", width=2)
            self._draw(c)
        tag = f"pn{n[0]}"
        n[0] += 1
        sym = ACTIONS[node.action][0]
        dashed = node.kind in ("module", "group", "root")
        st = self._status(node)
        outline, width = {"applying": ("#4aa3ff", 3), "done": ("#5fd35f", 3),
                          "error": ("#ff4d4d", 3)}.get(st, ("#aaa" if dashed else "#888", 1))
        cv.create_rectangle(node.x, node.y, node.x + BW, node.y + BH,
                            fill=FILL.get(node.action, "#2d2d2d"), outline=outline,
                            dash=(4, 2) if dashed and not st else None, width=width,
                            tags=(tag, "box"))
        if st:
            cv.create_text(node.x + BW - 8, node.y + 13, anchor="e",
                           text={"applying": "…", "done": "✓", "error": "✗"}[st],
                           fill=outline, font=("TkDefaultFont", 11, "bold"), tags=(tag,))
        head = {"root": "stack", "module": "module", "group": "", "output": "output"}.get(
            node.kind, "")
        top = f"{sym} {node.label}" if node.kind not in ("root", "module") else \
            f"{sym} {head}: {node.label}"
        cv.create_text(node.x + 8, node.y + 13, anchor="w", text=_clip(top, 31),
                       fill="#9cdcfe", font=("TkDefaultFont", 8, "bold"), tags=(tag,))
        cv.create_text(node.x + 8, node.y + 32, anchor="w", text=_clip(node.sub, 34),
                       fill="#ddd", font=("TkDefaultFont", 8), tags=(tag,))
        self.node_by_tag[tag] = node
        cv.tag_bind(tag, "<Button-1>", lambda _e, t=tag: self._map_click(t))

    def _status(self, node) -> str:
        """Apply status for a box; containers roll up their children."""
        if not self.apply_state:
            return ""
        if node.kind in ("resource", "data"):
            return self.apply_state.get(node.address, "")
        sts = [self._status(c) for c in node.children]
        if "error" in sts:
            return "error"
        if "applying" in sts:
            return "applying"
        changing = [c for c in node.children
                    if c.action not in ("no-op", "read") and c.kind != "output"]
        if changing and all(self._status(c) == "done" for c in changing):
            return "done"
        return ""

    def _map_click(self, tag):
        cv = self.canvas
        for item in cv.find_withtag("box"):
            if cv.itemcget(item, "outline") == "#ffffff":
                cv.itemconfigure(item, width=1, outline="#888")
        for item in cv.find_withtag(tag):
            if cv.type(item) == "rectangle":
                cv.itemconfigure(item, width=3, outline="#ffffff")
        self._show_node(self.node_by_tag.get(tag))

    def _render_list(self):
        lv = self.listview
        lv.delete(*lv.get_children(""))
        self.node_by_iid.clear()
        if self.tree_root is None:
            return

        def ins(node, parent):
            sym, label, _ = ACTIONS[node.action]
            text = f"{node.label} {node.sub}".strip() if node.kind in ("root", "module", "group") \
                else f"{node.label}.{node.sub}"
            mark = {"applying": "… ", "done": "✓ ", "error": "✗ "}.get(
                self._status(node), "")
            iid = lv.insert(parent, "end", open=True, text=mark + text,
                            values=(f"{sym} {label}", node.address, node.reason),
                            tags=(node.action,))
            self.node_by_iid[iid] = node
            for c in node.children:
                ins(c, iid)
        ins(self.tree_root, "")

    def _list_selected(self, _e=None):
        sel = self.listview.selection()
        if sel:
            self._show_node(self.node_by_iid.get(sel[0]))

    def _show_node(self, node):
        self._current = node
        self.attrs.delete(*self.attrs.get_children(""))
        if node is None:
            return
        self.side.select(0)
        sym, label, _ = ACTIONS[node.action]
        self.det_title.config(text=f"{sym} {node.address or node.label}")
        info = [f"action: {label}"]
        if node.reason:
            info.append(f"reason: {node.reason.replace('_', ' ')}")
        if node.change and node.change.get("replace_paths"):
            info.append("forces replacement: " + ", ".join(
                ".".join(str(p) for p in path) for path in node.change["replace_paths"]))
        if node.refs:
            info.append("references: " + ", ".join(node.refs))
        if node.kind in ("root", "module", "group"):
            info.append(f"contains: {node.sub}")
        self.det_info.config(text="\n".join(info))
        if node.change and node.kind in ("resource", "data"):
            for attr, before, after, changed in plan_ops.attribute_rows(node.change):
                if self.only_changed.get() and not changed:
                    continue
                tags = []
                if changed:
                    tags.append("chg")
                if after == "(known after apply)":
                    tags.append("unk")
                self.attrs.insert("", "end", text=attr, values=(before, after),
                                  tags=tuple(tags))
        elif node.change and node.kind == "output":
            for attr, before, after, changed in plan_ops.attribute_rows(
                    {"before": {"value": node.change.get("before")},
                     "after": {"value": node.change.get("after")},
                     "after_unknown": {"value": node.change.get("after_unknown")},
                     "before_sensitive": node.change.get("before_sensitive"),
                     "after_sensitive": node.change.get("after_sensitive")}):
                self.attrs.insert("", "end", text=attr, values=(before, after),
                                  tags=("chg",) if changed else ())

    def _render_problems(self):
        pv = self.probs
        pv.delete(*pv.get_children(""))
        self.prob_detail.delete("1.0", "end")
        for i, d in enumerate(self.diags):
            pv.insert("", "end", iid=str(i), values=(d.where or d.address or "—",
                                                     f"{d.severity}: {d.summary}"),
                      tags=(d.severity,))
        n_err = sum(d.severity == "error" for d in self.diags)
        n_warn = len(self.diags) - n_err
        self.side.tab(self.prob_tab, text=f"Problems ({n_err} ✗, {n_warn} ⚠)"
                      if self.diags else "Problems")

    def _diag_selected(self, _e=None):
        sel = self.probs.selection()
        if not sel:
            return
        d = self.diags[int(sel[0])]
        self.prob_detail.delete("1.0", "end")
        self.prob_detail.insert("1.0", f"{d.severity.upper()}: {d.summary}\n"
                                + (f"at {d.where}\n" if d.where else "")
                                + (f"resource {d.address}\n" if d.address else "")
                                + f"\n{d.detail}")


def is_bundle(folder: Path) -> bool:
    """A porter bundle, whatever the folder is called: providers/ + MANIFEST.json."""
    return (folder / "providers").is_dir() and (folder / "MANIFEST.json").is_file()


def find_bundle(stack: Path, bundle_var=None) -> Path | None:
    """Look for a bundle inside the stack, next to it, or next to its parent,
    then fall back to the Build Bundle screen's output folder."""
    places = [stack, stack.parent, stack.parent.parent]
    for base in places:
        try:
            kids = sorted(p for p in base.iterdir() if p.is_dir())
        except OSError:
            continue
        for k in kids:
            if is_bundle(k):
                return k
    if bundle_var is not None:
        b = Path(bundle_var.get() or ".")
        if is_bundle(b):
            return b
    return None


def wheel_units(delta: int, platform: str = sys.platform) -> int:
    """Scroll steps for a <MouseWheel> delta. Windows sends ±120 per notch;
    macOS sends small values (±1..±10, trackpads included), so dividing by 120
    there would always give 0 and the map would never scroll."""
    if platform == "darwin":
        return -delta
    return int(-delta / 120) or (-1 if delta > 0 else 1 if delta < 0 else 0)


def bind_wheel(widget):
    """Mouse-wheel / trackpad scrolling on Windows, macOS and Linux (X11 sends
    Button-4/5 instead of <MouseWheel>). Shift = horizontal."""
    widget.bind("<MouseWheel>", lambda e: widget.yview_scroll(wheel_units(e.delta), "units"))
    widget.bind("<Shift-MouseWheel>",
                lambda e: widget.xview_scroll(wheel_units(e.delta), "units"))
    widget.bind("<Button-4>", lambda e: widget.yview_scroll(-1, "units"))
    widget.bind("<Button-5>", lambda e: widget.yview_scroll(1, "units"))
    widget.bind("<Shift-Button-4>", lambda e: widget.xview_scroll(-1, "units"))
    widget.bind("<Shift-Button-5>", lambda e: widget.xview_scroll(1, "units"))


def _layout(node, depth, row):
    node.x = 12 + depth * (BW + HGAP)
    if node.children:
        for c in node.children:
            row = _layout(c, depth + 1, row)
        node.y = node.children[0].y        # top-aligned: tree reads from top-left
        return row
    node.y = 12 + row * (BH + VGAP)
    return row + 1


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n - 1] + "…"


class ConfirmApply(tk.Toplevel):
    """Modal confirmation: shows exactly what will happen and requires typing
    the stack folder name. .result is True only on a confirmed apply."""

    def __init__(self, master, cfg: plan_ops.RunConfig, plan: dict, wait: bool = True):
        super().__init__(master)
        self.result = False
        self.title("Apply plan — changes real infrastructure")
        self.transient(master.winfo_toplevel())
        self.geometry("660x540")
        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)
        tk.Label(body, text="⚠  This will change real infrastructure",
                 fg="#8a1010", font=("", 12, "bold")).pack(anchor="w")
        accts = plan_ops.plan_accounts(plan)
        facts = [("Stack", str(cfg.stack)),
                 ("Plan", plan_ops.summary_line(plan)),
                 ("Account(s)", ", ".join(accts) or "(not pinned by allowed_account_ids)"),
                 ("AWS profile", cfg.aws_profile or "(environment default)"),
                 ("Var files", "; ".join(cfg.var_files) or "(none)"),
                 ("State", "local state file in the stack folder" if cfg.local_backend
                  else "configured backend" + (f" ({cfg.backend_config})"
                                               if cfg.backend_config else ""))]
        grid = ttk.Frame(body)
        grid.pack(fill="x", pady=8)
        for i, (k, v) in enumerate(facts):
            ttk.Label(grid, text=k + ":", font=("", 9, "bold")).grid(
                row=i, column=0, sticky="nw", padx=(0, 8))
            ttk.Label(grid, text=v, wraplength=500, justify="left").grid(
                row=i, column=1, sticky="w")
        risky = plan_ops.risky_changes(plan)
        if risky:
            tk.Label(body, text=f"{len(risky)} resource(s) will be DESTROYED or REPLACED:",
                     fg="#8a1010", font=("", 10, "bold")).pack(anchor="w", pady=(6, 2))
            lb = tk.Listbox(body, height=min(8, len(risky)), fg="#8a1010")
            for addr, what in risky:
                lb.insert("end", f"{what:8}  {addr}")
            lb.pack(fill="both", expand=True)
        else:
            ttk.Label(body, text="Nothing will be destroyed or replaced.",
                      foreground="#163").pack(anchor="w", pady=(6, 2))
        ttk.Label(body, text=f"Type the stack folder name  {cfg.stack.name}  to confirm:"
                  ).pack(anchor="w", pady=(10, 2))
        self.typed = tk.StringVar()
        e = ttk.Entry(body, textvariable=self.typed)
        e.pack(fill="x")
        e.focus_set()
        btns = ttk.Frame(body)
        btns.pack(fill="x", pady=(12, 0))
        self.go = ttk.Button(btns, text="Apply", state="disabled", command=self._ok)
        self.go.pack(side="right")
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="right", padx=6)
        self.typed.trace_add("write", lambda *_: self.go.config(
            state="normal" if self.typed.get().strip() == cfg.stack.name else "disabled"))
        self.bind("<Escape>", lambda _e: self.destroy())
        if wait:
            self.grab_set()
            self.wait_window()

    def _ok(self):
        self.result = True
        self.destroy()
