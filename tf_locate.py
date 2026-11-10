"""
tf_locate — find the `terraform` (and `gpg`) binaries the app should use, the
same way on Windows, macOS and Linux.

Search order (first hit is the default; every hit is offered as a choice):

  1. PATH      — the PATH of the terminal the app was launched from
                 (shutil.which on this process's environment). If you start
                 the app from a shell where `terraform` works, this is it.
  2. shell     — macOS / Linux only, and only when 1 found nothing: the PATH of
                 your login shell ($SHELL -l -c). Apps started from Finder or a
                 desktop launcher get a minimal PATH without /opt/homebrew/bin,
                 tfenv shims, etc.; this recovers them.
  3. common    — well-known install folders (Homebrew, /usr/local/bin,
                 ~/.local/bin, tfenv, WinGet links/packages, Chocolatey, Scoop).
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
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

EXE = "terraform.exe" if sys.platform == "win32" else "terraform"


@dataclass(frozen=True)
class Candidate:
    path: str
    source: str          # PATH | shell | common | bundle | chosen
    version: str = ""    # "1.16.5" or "" if it could not be run

    @property
    def label(self) -> str:
        v = f"terraform {self.version}" if self.version else "terraform (version ?)"
        where = {"PATH": "from PATH", "shell": "from login-shell PATH",
                 "common": "common install folder", "bundle": "from bundle",
                 "chosen": "chosen"}.get(self.source, self.source)
        return f"{v} — {where} — {self.path}"


def _kw():
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


@lru_cache(maxsize=64)
def terraform_version(path: str) -> str:
    """'1.16.5' via `terraform version -json`; '' if it can't run."""
    try:
        p = subprocess.run([path, "version", "-json"], capture_output=True, text=True,
                           timeout=20, **_kw())
        return json.loads(p.stdout).get("terraform_version", "")
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""


@lru_cache(maxsize=1)
def login_shell_path() -> str:
    """PATH as your login shell sets it (macOS / Linux). '' on Windows or failure."""
    if sys.platform == "win32":
        return ""
    shell = os.environ.get("SHELL") or ("/bin/zsh" if sys.platform == "darwin" else "/bin/bash")
    # -l reads .zprofile/.bash_profile (Homebrew), -i also reads .zshrc/.bashrc
    # (tfenv, asdf, mise). Markers keep any banner those files print out of it.
    try:
        p = subprocess.run([shell, "-l", "-i", "-c", 'printf "@@PATH@@%s@@END@@" "$PATH"'],
                           capture_output=True, text=True, timeout=10,
                           stdin=subprocess.DEVNULL)
        out = p.stdout
        if "@@PATH@@" in out and "@@END@@" in out:
            return out.split("@@PATH@@", 1)[1].split("@@END@@", 1)[0]
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


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
            str(home / "bin"), str(home / ".tfenv" / "bin"), "/usr/bin", "/snap/bin"]
    if name == "gpg":
        dirs += ["/usr/local/MacGPG2/bin"]
    return dirs


def _exe(name: str) -> str:
    return name + (".exe" if sys.platform == "win32" and not name.endswith(".exe") else "")


def which_in(name: str, path_str: str) -> str | None:
    return shutil.which(name, path=path_str) if path_str else None


def find_all(bundle: Path | None = None, host_platform: str = "") -> list[Candidate]:
    """Every terraform we can find, in priority order, de-duplicated."""
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
        add(which_in("terraform", login_shell_path()), "shell")
    for d in _common_dirs("terraform"):
        add(os.path.join(d, _exe("terraform")), "common")
    if bundle and host_platform:
        add(str(Path(bundle) / "bin" / host_platform / EXE), "bundle")
    return out


def default_terraform(bundle: Path | None = None, host_platform: str = "") -> Candidate | None:
    c = find_all(bundle, host_platform)
    return c[0] if c else None


def find_gpg() -> str | None:
    """gpg: PATH, then login-shell PATH, then common folders."""
    hit = shutil.which("gpg") or which_in("gpg", login_shell_path())
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
