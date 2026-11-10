"""
tf_locate — find the `terraform` (and `gpg`) binaries the app should use, the
same way on Windows, macOS and Linux.

Search order (first hit is the default; every hit is offered as a choice):

  1. PATH      — the PATH of the terminal the app was launched from
                 (shutil.which on this process's environment). If you start
                 the app from a shell where `terraform` works, this is it.
  2. system    — macOS only, when 1 found nothing: the folders macOS's own
                 path_helper puts on every Terminal PATH (/etc/paths and
                 /etc/paths.d/*). Apps started from Finder get a minimal PATH;
                 this recovers it by READING those files — no shell is started.
  3. common    — well-known install folders (Homebrew, /usr/local/bin,
                 ~/.local/bin, tfenv, asdf, mise, WinGet links/packages,
                 Chocolatey, Scoop).

Nothing here launches a shell or touches protected folders (Documents,
Desktop, iCloud): an earlier version ran `$SHELL -l -i` to read your login
PATH, which on macOS executed all of ~/.zshrc — slow, and it could trigger
"would like to access…" privacy prompts. Detection runs once (cached; ⟳ in
the UI refreshes) and `terraform version` runs with CHECKPOINT_DISABLE=1 so it
never phones home for an update check.
  4. bundle    — <bundle>/bin/<os>_<arch>/terraform[.exe] from a porter bundle.

Each candidate records where it came from and its `terraform version`, so the
UI can say e.g. "terraform 1.16.5 — from PATH".

Standard library only.
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

EXE = "terraform.exe" if sys.platform == "win32" else "terraform"


@dataclass(frozen=True)
class Candidate:
    path: str
    source: str          # PATH | system | common | bundle | chosen
    version: str = ""    # "1.16.5" or "" if it could not be run

    @property
    def label(self) -> str:
        v = f"terraform {self.version}" if self.version else "terraform (version ?)"
        where = {"PATH": "from PATH", "system": "from macOS system PATH",
                 "common": "common install folder", "bundle": "from bundle",
                 "chosen": "chosen"}.get(self.source, self.source)
        return f"{v} — {where} — {self.path}"


def _kw():
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


@lru_cache(maxsize=64)
def terraform_version(path: str) -> str:
    """'1.16.5' via `terraform version -json`; '' if it can't run.
    CHECKPOINT_DISABLE stops terraform's online update check (which otherwise
    waits on the network — and times out in an air gap)."""
    env = dict(os.environ, CHECKPOINT_DISABLE="1")
    try:
        p = subprocess.run([path, "version", "-json"], capture_output=True, text=True,
                           timeout=20, env=env, stdin=subprocess.DEVNULL, **_kw())
        return json.loads(p.stdout).get("terraform_version", "")
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""


@lru_cache(maxsize=1)
def system_path() -> str:
    """macOS: the PATH Terminal gets from path_helper — /etc/paths plus every
    file in /etc/paths.d — read directly (no shell). '' elsewhere."""
    if sys.platform != "darwin":
        return ""
    dirs: list[str] = []
    files = [Path("/etc/paths")]
    try:
        files += sorted(Path("/etc/paths.d").iterdir())
    except OSError:
        pass
    for f in files:
        try:
            for ln in f.read_text(encoding="utf-8", errors="replace").splitlines():
                ln = ln.strip()
                if ln and not ln.startswith("#") and ln not in dirs:
                    dirs.append(ln)
        except OSError:
            continue
    return os.pathsep.join(dirs)


def _common_dirs(name: str) -> list[str]:
    home = Path.home()
    if sys.platform == "win32":
        la = os.environ.get("LOCALAPPDATA", str(home / "AppData" / "Local"))
        dirs = [os.path.join(la, "Microsoft", "WinGet", "Links"),
                *sorted(glob.glob(os.path.join(la, "Microsoft", "WinGet", "Packages",
                                               "Hashicorp.Terraform*"))),
                r"C:\ProgramData\chocolatey\bin",
                str(home / "scoop" / "shims"),
                r"C:\HashiCorp\Terraform", r"C:\terraform"]
        if name == "gpg":
            pf = os.environ.get("ProgramFiles", r"C:\Program Files")
            pfx = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
            dirs = [os.path.join(pf, "Git", "usr", "bin"), os.path.join(pf, "GnuPG", "bin"),
                    os.path.join(pfx, "GnuPG", "bin")]
        return dirs
    dirs = ["/opt/homebrew/bin", "/usr/local/bin", str(home / ".local" / "bin"),
            str(home / "bin"), str(home / ".tfenv" / "bin"), str(home / ".asdf" / "shims"),
            str(home / ".local" / "share" / "mise" / "shims"), "/usr/bin", "/snap/bin"]
    if name == "gpg":
        dirs += ["/usr/local/MacGPG2/bin"]
    return dirs


def _exe(name: str) -> str:
    return name + (".exe" if sys.platform == "win32" and not name.endswith(".exe") else "")


def which_in(name: str, path_str: str) -> str | None:
    return shutil.which(name, path=path_str) if path_str else None


_cache: dict = {}
_lock = threading.Lock()


def find_all(bundle: Path | None = None, host_platform: str = "",
             refresh: bool = False) -> list[Candidate]:
    """Every terraform we can find, in priority order, de-duplicated. Cached:
    the startup log and the Plan Explorer share one search; refresh=True
    (the ⟳ button) searches again."""
    key = (str(bundle or ""), host_platform)
    with _lock:
        if refresh:
            _cache.clear()
            terraform_version.cache_clear()
        if key not in _cache:
            _cache[key] = _search(bundle, host_platform)
        return list(_cache[key])


def _search(bundle: Path | None, host_platform: str) -> list[Candidate]:
    out: list[Candidate] = []
    seen: set[str] = set()

    def add(p, source):
        if not p:
            return
        key = os.path.normcase(os.path.realpath(p))
        if key in seen or not os.path.isfile(p):
            return
        seen.add(key)
        out.append(Candidate(str(p), source, terraform_version(str(p))))

    add(shutil.which("terraform"), "PATH")
    if not out:
        add(which_in("terraform", system_path()), "system")
    for d in _common_dirs("terraform"):
        add(os.path.join(d, _exe("terraform")), "common")
    if bundle and host_platform:
        add(str(Path(bundle) / "bin" / host_platform / EXE), "bundle")
    return out


def default_terraform(bundle: Path | None = None, host_platform: str = "") -> Candidate | None:
    c = find_all(bundle, host_platform)
    return c[0] if c else None


@lru_cache(maxsize=1)
def find_gpg() -> str | None:
    """gpg: PATH, then macOS system PATH, then common folders. Cached."""
    hit = shutil.which("gpg") or which_in("gpg", system_path())
    if hit:
        return hit
    for d in _common_dirs("gpg"):
        p = os.path.join(d, _exe("gpg"))
        if os.path.isfile(p):
            return p
    return None


def describe(path: str) -> Candidate:
    """Candidate for a path the user typed / browsed to."""
    src = "chosen"
    on_path = shutil.which("terraform")
    if on_path and os.path.normcase(os.path.realpath(on_path)) == \
            os.path.normcase(os.path.realpath(path)):
        src = "PATH"
    return Candidate(path, src, terraform_version(path))
