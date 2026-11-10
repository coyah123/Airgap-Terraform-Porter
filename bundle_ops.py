"""
bundle_ops — turn a scan into a concrete, reviewable plan of steps, and the
steps themselves: download + verify the Terraform CLI and every provider for
every chosen platform, lay them out as a filesystem mirror, optionally refresh
lock files for all platforms, and write the CLI configs, install scripts,
manifest and checksums.

Bundle layout (what gets carried across the air gap):

    <bundle>/
      bin/<os>_<arch>/terraform[.exe]          extracted CLI, ready to run
      terraform/terraform_<v>_<os>_<arch>.zip  original release zips
      providers/<host>/<ns>/<type>/terraform-provider-<type>_<v>_<os>_<arch>.zip
                                               ^ packed filesystem-mirror layout
      checksums/...                            upstream SHA256SUMS + .sig + key
      terraform.rc   .terraformrc              CLI configs (mirror only, no direct)
      install.ps1    install.sh                verify + install on the far side
      INSTALL.md     MANIFEST.json   SHA256SUMS.txt

A "step" is (label, action, detail): action is a callable(emit) -> exit code,
or an argv list with an optional cwd (see porter.JobRunner). detail is what the
Preview screen shows (URL / command).

Standard library only.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import shutil
import stat
import subprocess
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import fetch_ops
import scan_ops
from tf_versions import newest_matching, allows_all

TOOL = "Air-Gapped Terraform Porter"
TOOL_VERSION = "1.0.0"

# Offered on the Platforms screen. First three on by default (the brief).
KNOWN_PLATFORMS = ["linux_amd64", "linux_arm64", "windows_amd64",
                   "darwin_arm64", "darwin_amd64", "windows_arm64"]
DEFAULT_PLATFORMS = ["linux_amd64", "linux_arm64", "windows_amd64"]

# Where the shipped CLI configs point if the bundle is copied there verbatim.
# The install scripts rewrite the path to wherever the bundle actually lives.
DEFAULT_WIN_HOME = "C:/terraform-airgap"
DEFAULT_LINUX_HOME = "/opt/terraform-airgap"


@dataclass
class ProviderPick:
    address: str
    version: str
    platforms: list[str]          # chosen AND published for this version
    missing: list[str]            # chosen but not published -> skipped
    why: str                      # "locked" / "resolved from <constraints>"

    @property
    def type(self):
        return self.address.split("/")[2]


@dataclass
class Plan:
    scan: scan_ops.ScanResult
    bundle: Path
    platforms: list[str]
    tf_version: str | None
    tf_platforms: list[str]
    providers: list[ProviderPick]
    verify_gpg: bool
    gpg: str | None
    update_locks: bool
    lock_source: str              # "bundle" (offline, from the mirror) | "registry"
    lock_terraform: str | None    # terraform binary used for providers lock
    warnings: list[str] = field(default_factory=list)
    # filled in while the steps run, written to MANIFEST.json at the end
    manifest: dict = field(default_factory=dict)


# -- resolution (network: version lists only, no big downloads) --------------
def resolve(scan: scan_ops.ScanResult, bundle: Path, platforms: list[str],
            tf_version: str = "", verify_gpg: bool = True,
            update_locks: bool = True, lock_source: str = "bundle",
            include_cli: bool = True, terraform: str = "") -> Plan:
    warnings = list(scan.warnings)
    platforms = [p.strip() for p in platforms if p.strip()]

    # Terraform core
    tfv = None
    tf_plats: list[str] = []
    if include_cli:
        all_v = fetch_ops.terraform_versions()
        if tf_version.strip():
            tfv = tf_version.strip().lstrip("v")
            if tfv not in all_v:
                raise RuntimeError(f"Terraform {tfv} is not on releases.hashicorp.com")
            if not allows_all(scan.core_constraints, tfv):
                warnings.append(f"Terraform {tfv} does NOT satisfy required_version "
                                f"{', '.join(scan.core_constraints)}")
        else:
            tfv = newest_matching(all_v, scan.core_constraints)
            if not tfv:
                raise RuntimeError("no Terraform release satisfies "
                                   + ", ".join(scan.core_constraints))
        builds = fetch_ops.terraform_builds(tfv)
        tf_plats = [p for p in platforms if p in builds]
        for p in platforms:
            if p not in builds:
                warnings.append(f"Terraform {tfv} has no {p} build — skipped")

    # Providers
    picks: list[ProviderPick] = []
    for addr, req in scan.providers.items():
        exact, needs = scan_ops.versions_to_mirror(req, scan.roots)
        chosen: dict[str, str] = {v: "locked" for v in exact}
        if needs:
            avail = [v["version"] for v in fetch_ops.provider_versions(addr)]
            best = newest_matching(avail, sorted(req.constraints))
            if best:
                chosen.setdefault(best, "resolved from "
                                  + (", ".join(sorted(req.constraints)) or "no constraint"))
            else:
                warnings.append(f"{req.short}: no published version satisfies "
                                + ", ".join(sorted(req.constraints)))
        for ver, why in sorted(chosen.items()):
            pub = fetch_ops.provider_platforms(addr, ver)
            if not pub:
                warnings.append(f"{req.short} {ver}: not found in the registry")
                continue
            have = [p for p in platforms if p in pub]
            miss = [p for p in platforms if p not in pub]
            for p in miss:
                warnings.append(f"{req.short} {ver}: no {p} build published — skipped")
            picks.append(ProviderPick(addr, ver, have, miss, why))

    gpg = fetch_ops.find_gpg() if verify_gpg else None
    if verify_gpg and not gpg:
        warnings.append("gpg not found — signatures will NOT be checked "
                        "(SHA256 checksums still are)")

    lock_tf = None
    if update_locks:
        # Same rule as everywhere: an explicit choice, else terraform from the
        # launching terminal's PATH (tf_locate), else this machine's bundled one
        # (downloaded by an earlier step of this plan).
        import tf_locate
        if terraform:
            lock_tf = terraform
        else:
            best = tf_locate.default_terraform()
            lock_tf = best.path if best else None
        if not lock_tf and tfv:
            host = host_platform()
            if host in tf_plats:
                lock_tf = str(bundle / "bin" / host / exe_name(host))
        if not lock_tf:
            warnings.append("no terraform binary available to run `providers "
                            "lock` — lock files will not be updated")

    return Plan(scan, bundle, platforms, tfv, tf_plats, picks,
                verify_gpg and bool(gpg), gpg, update_locks and bool(lock_tf),
                lock_source, lock_tf, warnings)


def host_platform() -> str:
    import platform as _p
    m = _p.machine().lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64",
            "arm64": "arm64"}.get(m, m)
    os_ = {"win32": "windows", "darwin": "darwin"}.get(sys.platform, "linux")
    return f"{os_}_{arch}"


def exe_name(platform: str) -> str:
    return "terraform.exe" if platform.startswith("windows") else "terraform"


def provider_zip_rel(addr: str, version: str, platform: str) -> Path:
    host, ns, typ = addr.split("/")
    return Path("providers", host, ns, typ,
                f"terraform-provider-{typ}_{version}_{platform}.zip")


# -- steps -------------------------------------------------------------------
def build_steps(plan: Plan) -> list:
    b = plan.bundle
    plan.manifest = {
        "tool": TOOL, "tool_version": TOOL_VERSION,
        "created": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "source_dir": str(plan.scan.root),
        "platforms": plan.platforms,
        "signature_checks": "gpg" if plan.verify_gpg else "none (sha256 only)",
        "terraform": None, "providers": [], "roots": [], "lock_files": [],
        "warnings": plan.warnings,
    }
    steps = [("prepare bundle folder", _step_prepare(plan), str(b))]
    if plan.tf_version:
        plan.manifest["terraform"] = {
            "version": plan.tf_version,
            "required_version": plan.scan.core_constraints, "builds": []}
        for p in plan.tf_platforms:
            u = fetch_ops.terraform_urls(plan.tf_version, p)
            steps.append((f"terraform {plan.tf_version} {p}",
                          _step_terraform(plan, p), u["zip"]))
    for pick in plan.providers:
        for p in pick.platforms:
            steps.append((f"{pick.address.split('/', 1)[1]} {pick.version} {p}",
                          _step_provider(plan, pick, p),
                          f"registry {pick.address} {pick.version} {p}"))
    if plan.update_locks:
        for root in plan.scan.roots:
            argv = lock_argv(plan, root)
            steps.append((f"lock all platforms: {_rel(plan, root.path)}",
                          (argv, str(root.path)), " ".join(argv)))
        steps.append(("check lock files cover every platform",
                      _step_check_locks(plan), "(compare lock h1 hashes to bundle)"))
    steps.append(("write CLI configs, install scripts, manifest",
                  _step_finish(plan), "terraform.rc  .terraformrc  install.*  MANIFEST.json"))
    return steps


def lock_argv(plan: Plan, root: scan_ops.ModuleDir) -> list[str]:
    argv = [plan.lock_terraform, "providers", "lock"]
    argv += [f"-platform={p}" for p in plan.platforms]
    if plan.lock_source == "bundle":
        argv.append(f"-fs-mirror={(plan.bundle / 'providers').resolve()}")
    return argv


def _rel(plan, p: Path) -> str:
    try:
        return str(p.relative_to(plan.scan.root)) or "."
    except ValueError:
        return str(p)


def _step_prepare(plan: Plan):
    def run(emit):
        b = plan.bundle
        for sub in ("bin", "terraform", "providers", "checksums"):
            (b / sub).mkdir(parents=True, exist_ok=True)
        emit(f"bundle: {b.resolve()}")
        if plan.verify_gpg:
            key = fetch_ops.hashicorp_key()
            (b / "checksums" / "hashicorp.asc").write_text(key, encoding="utf-8", newline="\n")
            emit(f"HashiCorp release key fetched (expect fingerprint {fetch_ops.HASHICORP_FPR})")
            emit(f"gpg: {plan.gpg}")
        return 0
    return run


def _verified_sums(plan, emit, sums_url, sig_url, dest_dir: Path, keys, expect_fpr):
    """Download SHA256SUMS (+sig) into dest_dir once; verify signature."""
    sums_path = dest_dir / Path(sums_url.split("?")[0]).name
    sig_path = dest_dir / Path(sig_url.split("?")[0]).name if sig_url else None
    if not sums_path.exists():
        fetch_ops.download_file(sums_url, sums_path, emit)
        if sig_path:
            fetch_ops.download_file(sig_url, sig_path, emit)
    fpr = None
    if plan.verify_gpg:
        if not sig_path:
            raise RuntimeError("no signature published for " + sums_url)
        fpr = fetch_ops.gpg_verify(plan.gpg, keys, sums_path, sig_path)
        if expect_fpr and fpr != expect_fpr:
            raise RuntimeError(f"signed by {fpr}, expected {expect_fpr}")
        emit(f"  ✓ signature OK ({sums_path.name}, key {fpr[-16:]})")
    return fetch_ops.parse_shasums(sums_path.read_text(encoding="utf-8")), sums_path, fpr


def _step_terraform(plan: Plan, platform: str):
    def run(emit):
        b, v = plan.bundle, plan.tf_version
        u = fetch_ops.terraform_urls(v, platform)
        keys = [fetch_ops.hashicorp_key()] if plan.verify_gpg else []
        sums, sums_path, fpr = _verified_sums(
            plan, emit, u["sums"], u["sig"], b / "checksums" / "terraform" / v,
            keys, fetch_ops.HASHICORP_FPR)
        zpath = b / "terraform" / u["zip_name"]
        if not (zpath.exists() and fetch_ops.sha256_file(zpath) == sums.get(u["zip_name"])):
            emit(f"  GET {u['zip']}")
            fetch_ops.download_file(u["zip"], zpath, emit)
        digest = fetch_ops.sha256_file(zpath)
        if digest != sums.get(u["zip_name"]):
            zpath.unlink(missing_ok=True)
            raise RuntimeError(f"sha256 mismatch for {u['zip_name']}")
        emit(f"  ✓ sha256 {digest[:16]}… matches SHA256SUMS")
        out_dir = b / "bin" / platform
        out_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zpath) as z:
            z.extractall(out_dir)
        exe = out_dir / exe_name(platform)
        if not platform.startswith("windows"):
            exe.chmod(exe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        plan.manifest["terraform"]["builds"].append({
            "platform": platform,
            "zip": zpath.relative_to(b).as_posix(), "zip_sha256": digest,
            "binary": exe.relative_to(b).as_posix(),
            "binary_sha256": fetch_ops.sha256_file(exe),
            "sha256sums": sums_path.relative_to(b).as_posix(),
            "signed_by": fpr,
        })
        emit(f"  → {exe.relative_to(b).as_posix()}")
        return 0
    return run


def _step_provider(plan: Plan, pick: ProviderPick, platform: str):
    def run(emit):
        b = plan.bundle
        info = fetch_ops.provider_download(pick.address, pick.version, platform)
        host, ns, typ = pick.address.split("/")
        keys = [k["ascii_armor"] for k in
                (info.get("signing_keys") or {}).get("gpg_public_keys", [])]
        expect = None
        if host == scan_ops.DEFAULT_HOST and ns == "hashicorp":
            expect = fetch_ops.HASHICORP_FPR
            if plan.verify_gpg:
                keys = [fetch_ops.hashicorp_key()]   # pinned, not registry-supplied
        if plan.verify_gpg and not keys:
            raise RuntimeError("registry returned no signing key")
        sums, sums_path, fpr = _verified_sums(
            plan, emit, info["shasums_url"], info.get("shasums_signature_url"),
            b / "checksums" / "providers" / host / ns / typ / pick.version, keys, expect)
        if expect is None and plan.verify_gpg:
            key_ids = [k.get("key_id", "").upper() for k in
                       (info.get("signing_keys") or {}).get("gpg_public_keys", [])]
            if key_ids and not any(fpr.endswith(k) for k in key_ids if k):
                emit(f"  note: signed by primary key {fpr}; registry key ids {key_ids}")
        fname = info["filename"]
        rel = provider_zip_rel(pick.address, pick.version, platform)
        if Path(fname).name != rel.name:
            emit(f"  note: registry filename {fname} stored as {rel.name}")
        zpath = b / rel
        expected = (info.get("shasum") or "").lower()
        if sums.get(fname) and expected and sums[fname] != expected:
            raise RuntimeError("registry shasum disagrees with SHA256SUMS")
        expected = expected or sums.get(fname, "")
        if not (zpath.exists() and fetch_ops.sha256_file(zpath) == expected):
            emit(f"  GET {info['download_url']}")
            fetch_ops.download_file(info["download_url"], zpath, emit)
        digest = fetch_ops.sha256_file(zpath)
        if digest != expected or sums.get(fname) != digest:
            zpath.unlink(missing_ok=True)
            raise RuntimeError(f"sha256 mismatch for {fname}")
        h1 = fetch_ops.h1_of_zip(zpath)
        emit(f"  ✓ sha256 {digest[:16]}… matches SHA256SUMS; {h1}")
        plan.manifest["providers"].append({
            "address": pick.address, "version": pick.version, "platform": platform,
            "why": pick.why, "file": rel.as_posix(), "sha256": digest,
            "lock_hashes": {"zh": f"zh:{digest}", "h1": h1},
            "protocols": info.get("protocols"),
            "sha256sums": sums_path.relative_to(b).as_posix(), "signed_by": fpr,
        })
        return 0
    return run


def _step_check_locks(plan: Plan):
    def run(emit):
        bad = 0
        want: dict[tuple, set] = {}
        for e in plan.manifest["providers"]:
            want.setdefault((e["address"], e["version"]), set()).add(
                (e["platform"], e["lock_hashes"]["h1"], e["lock_hashes"]["zh"]))
        for root in plan.scan.roots:
            lock = root.path / scan_ops.LOCK_NAME
            entry = {"root": _rel(plan, root.path), "lock_file": str(lock),
                     "providers": {}}
            if not lock.exists():
                emit(f"  ✗ {entry['root']}: no lock file written")
                bad += 1
                plan.manifest["lock_files"].append(entry)
                continue
            locked = scan_ops.parse_lock_file(lock)
            for addr, le in locked.items():
                have = set(le["hashes"])
                rows = want.get((addr, le["version"]), set())
                covered = sorted(p for p, h1, zh in rows if h1 in have or zh in have)
                missing = sorted(p for p, h1, zh in rows if h1 not in have and zh not in have)
                entry["providers"][addr] = {"version": le["version"],
                                            "platforms_covered": covered,
                                            "platforms_missing": missing}
                if missing or not rows:
                    bad += 1
                    emit(f"  ✗ {entry['root']}: {addr} {le['version']} "
                         + (f"missing {', '.join(missing)}" if rows else
                            "is not in the bundle"))
                else:
                    emit(f"  ✓ {entry['root']}: {addr.split('/', 1)[1]} "
                         f"{le['version']} → {', '.join(covered)}")
            plan.manifest["lock_files"].append(entry)
        return 1 if bad else 0
    return run


def _step_finish(plan: Plan):
    def run(emit):
        b = plan.bundle
        for r in plan.scan.roots:
            plan.manifest["roots"].append({
                "path": _rel(plan, r.path),
                "required_version": r.required_versions,
                "providers": r.providers,
                "lock_file": bool((r.path / scan_ops.LOCK_NAME).exists()),
            })
        for p in write_mirror_index(b, plan.manifest["providers"]):
            emit(f"  wrote {p.relative_to(b).as_posix()}")
        files = {
            "terraform.rc": cli_config(f"{DEFAULT_WIN_HOME}/providers"),
            ".terraformrc": cli_config(f"{DEFAULT_LINUX_HOME}/providers"),
            "install.ps1": INSTALL_PS1,
            "install.sh": INSTALL_SH,
            "INSTALL.md": install_md(plan),
        }
        for name, text in files.items():
            p = b / name
            # LF everywhere except the Windows-only files.
            nl = "\r\n" if name in ("terraform.rc", "install.ps1") else "\n"
            p.write_bytes(text.replace("\n", nl).encode("utf-8"))
            emit(f"  wrote {name}")
        (b / "install.sh").chmod(0o755)
        (b / "MANIFEST.json").write_text(json.dumps(plan.manifest, indent=2) + "\n",
                                         encoding="utf-8", newline="\n")
        emit("  wrote MANIFEST.json")
        lines = []
        for p in sorted(b.rglob("*")):
            # installed.* are written by install.ps1/.sh on the far side.
            if p.is_file() and p.name != "SHA256SUMS.txt" \
                    and not p.name.startswith("installed."):
                lines.append(f"{fetch_ops.sha256_file(p)}  {p.relative_to(b).as_posix()}")
        (b / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        emit(f"  wrote SHA256SUMS.txt ({len(lines)} files)")
        total = sum(p.stat().st_size for p in b.rglob("*") if p.is_file())
        emit(f"  bundle size: {total / (1 << 20):.0f} MB")
        return 0
    return run


def write_mirror_index(bundle: Path, entries: list[dict]) -> list[Path]:
    """Write the JSON index files `terraform providers mirror` produces:

        <host>/<ns>/<type>/index.json        {"versions": {"5.100.0": {}}}
        <host>/<ns>/<type>/<version>.json    {"archives": {"linux_amd64":
                                               {"url": "<zip>", "hashes": ["h1:…"]}}}

    A filesystem_mirror ignores them, but with them the providers/ folder is
    the same layout that command writes, and can be served as-is over HTTPS
    as a `network_mirror` (nginx / S3), which requires them."""
    by_type: dict[Path, dict[str, dict]] = {}
    for e in entries:
        d = bundle / Path(e["file"]).parent
        by_type.setdefault(d, {}).setdefault(e["version"], {})[e["platform"]] = {
            "url": Path(e["file"]).name, "hashes": [e["lock_hashes"]["h1"]]}
    written = []
    for d, versions in sorted(by_type.items()):
        idx = d / "index.json"
        # sort_keys + no trailing newline = byte-identical to what
        # `terraform providers mirror` writes.
        idx.write_text(json.dumps({"versions": {v: {} for v in sorted(versions)}},
                                  indent=2, sort_keys=True),
                       encoding="utf-8", newline="\n")
        written.append(idx)
        for v, archives in sorted(versions.items()):
            vp = d / f"{v}.json"
            vp.write_text(json.dumps({"archives": archives}, indent=2, sort_keys=True),
                          encoding="utf-8", newline="\n")
            written.append(vp)
    return written


# -- generated text ----------------------------------------------------------
def cli_config(mirror_path: str) -> str:
    return f'''# Terraform CLI configuration — air-gapped.
# Generated by {TOOL}. Providers come ONLY from the local filesystem mirror;
# `direct` excludes every provider, so Terraform never contacts a registry.
#
# Use it by placing this file at the default location
#   Windows: %APPDATA%\\terraform.rc     Linux/macOS: ~/.terraformrc
# or by pointing TF_CLI_CONFIG_FILE at it. install.ps1 / install.sh do this
# for you and rewrite `path` to wherever the bundle really lives.

provider_installation {{
  filesystem_mirror {{
    path    = "{mirror_path}"
    include = ["*/*/*"]
  }}
  direct {{
    exclude = ["*/*/*"]
  }}
}}

# No version-check / telemetry calls home.
disable_checkpoint = true
'''


def install_md(plan: Plan) -> str:
    prov_lines = []
    seen = set()
    for e in plan.manifest["providers"]:
        key = (e["address"], e["version"])
        if key in seen:
            continue
        seen.add(key)
        plats = [x["platform"] for x in plan.manifest["providers"]
                 if (x["address"], x["version"]) == key]
        prov_lines.append(f"| `{e['address']}` | {e['version']} | {', '.join(plats)} |")
    tf = plan.manifest.get("terraform") or {}
    tf_plats = ", ".join(x["platform"] for x in tf.get("builds", [])) or "(none)"
    return f"""# Terraform air-gap bundle

Built {plan.manifest['created']} by {TOOL} {TOOL_VERSION} from `{plan.scan.root}`.

* Terraform CLI **{tf.get('version', 'not included')}** for {tf_plats}
* Signature checks at build time: {plan.manifest['signature_checks']}

| Provider | Version | Platforms |
|---|---|---|
{chr(10).join(prov_lines)}

## Install — Linux / macOS

```bash
cd /path/to/this/bundle
bash install.sh            # verifies SHA256SUMS.txt, installs terraform,
                           # writes ~/.terraformrc pointing at ./providers
terraform version
```

Options: `TF_BIN_DIR=/usr/local/bin bash install.sh` (default `~/.local/bin`,
or `/usr/local/bin` as root); `bash install.sh --no-rc` to skip writing
`~/.terraformrc` and instead `export TF_CLI_CONFIG_FILE=$PWD/installed.terraformrc`
(the installer always writes that file with the mirror path filled in).

## Install — Windows (PowerShell)

```powershell
cd C:\\path\\to\\this\\bundle
powershell -ExecutionPolicy Bypass -File .\\install.ps1
# verifies SHA256SUMS.txt, copies terraform.exe to %LOCALAPPDATA%\\terraform-airgap\\bin
# (added to your user PATH), writes %APPDATA%\\terraform.rc pointing at .\\providers
terraform version
```

`-BinDir <dir>` changes where terraform.exe goes, `-NoPath` leaves your PATH
alone, `-NoRc` skips `%APPDATA%\\terraform.rc`; then set
`$env:TF_CLI_CONFIG_FILE = "$PWD\\installed.terraform.rc"` instead (always
written, with the mirror path filled in).

## Use

Nothing in your `.tf` files changes — `source = "hashicorp/aws"` stays as is.
The CLI config redirects every provider to the local mirror:

```
terraform init        # installs providers from the bundle, no network
```

If you keep the bundle's own config files instead of running the installer,
copy the bundle to `{DEFAULT_WIN_HOME}` (Windows) or `{DEFAULT_LINUX_HOME}`
(Linux) — that is where the shipped `terraform.rc` / `.terraformrc` point.

Every file's checksum is in `SHA256SUMS.txt`; `MANIFEST.json` lists versions,
platforms, upstream checksums, signing keys and lock-file coverage.
"""


INSTALL_SH = r'''#!/usr/bin/env bash
# Air-gapped Terraform bundle installer (Linux / macOS). Needs no network.
#   bash install.sh [--no-rc]
#   TF_BIN_DIR=/usr/local/bin bash install.sh
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WRITE_RC=1
[ "${1:-}" = "--no-rc" ] && WRITE_RC=0

echo "== verifying bundle checksums"
cd "$HERE"
# sha256sum on Linux; macOS has `shasum` (Perl) — older versions lack
# --quiet, so filter the OK lines instead. Works with macOS's bash 3.2.
if command -v sha256sum >/dev/null 2>&1; then
  sum_check() { sha256sum -c SHA256SUMS.txt; }
else
  sum_check() { shasum -a 256 -c SHA256SUMS.txt; }
fi
if ! out="$(sum_check 2>&1)"; then
  printf '%s\n' "$out" | grep -v ': OK$' || true
  echo "   bundle is damaged or altered — not installing"; exit 1
fi
echo "   all files OK"

os="$(uname -s | tr '[:upper:]' '[:lower:]')"
if [ "$os" = darwin ] && command -v xattr >/dev/null 2>&1; then
  # Files copied on a Mac can carry Gatekeeper's quarantine flag, which stops
  # terraform / provider binaries from running. They were verified above.
  xattr -dr com.apple.quarantine "$HERE" 2>/dev/null || true
fi
case "$(uname -m)" in
  x86_64|amd64) arch=amd64 ;;
  aarch64|arm64) arch=arm64 ;;
  *) echo "unsupported CPU: $(uname -m)"; exit 1 ;;
esac
plat="${os}_${arch}"
src="$HERE/bin/$plat/terraform"
# -f, not -x: copies made on Windows / FAT media lose the exec bit; `install
# -m 0755` below restores it.
[ -f "$src" ] || { echo "no terraform for $plat in this bundle"; exit 1; }

if [ -z "${TF_BIN_DIR:-}" ]; then
  if [ "$(id -u)" = 0 ]; then TF_BIN_DIR=/usr/local/bin; else TF_BIN_DIR="$HOME/.local/bin"; fi
fi
mkdir -p "$TF_BIN_DIR"
install -m 0755 "$src" "$TF_BIN_DIR/terraform"
echo "== installed $TF_BIN_DIR/terraform ($plat)"
case ":$PATH:" in *":$TF_BIN_DIR:"*) ;; *) echo "   add to PATH: export PATH=\"$TF_BIN_DIR:\$PATH\"" ;; esac

# Generate a config pointing at this bundle's real location (the shipped
# .terraformrc is left untouched so SHA256SUMS.txt keeps verifying), then
# (optionally) install it as ~/.terraformrc.
rc="$HERE/installed.terraformrc"
sed "s#^    path    = \".*\"#    path    = \"$HERE/providers\"#" "$HERE/.terraformrc" > "$rc"
if [ "$WRITE_RC" = 1 ]; then
  if [ -f "$HOME/.terraformrc" ] && ! cmp -s "$HOME/.terraformrc" "$rc"; then
    cp "$HOME/.terraformrc" "$HOME/.terraformrc.bak.$(date +%Y%m%d%H%M%S)"
    echo "   backed up existing ~/.terraformrc"
  fi
  cp "$rc" "$HOME/.terraformrc"
  echo "== wrote ~/.terraformrc (mirror: $HERE/providers)"
else
  echo "== skipped ~/.terraformrc; use:  export TF_CLI_CONFIG_FILE=\"$rc\""
fi
"$TF_BIN_DIR/terraform" version
'''


INSTALL_PS1 = r'''# Air-gapped Terraform bundle installer (Windows). Needs no network.
#   powershell -ExecutionPolicy Bypass -File .\install.ps1 [-BinDir <dir>] [-NoRc] [-NoPath]
param(
    [string]$BinDir = (Join-Path $env:LOCALAPPDATA 'terraform-airgap\bin'),
    [switch]$NoRc,      # don't write %APPDATA%\terraform.rc
    [switch]$NoPath     # don't add BinDir to the user PATH
)
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host '== verifying bundle checksums'
$bad = 0
foreach ($line in Get-Content (Join-Path $here 'SHA256SUMS.txt')) {
    if (-not $line.Trim()) { continue }
    $hash, $rel = $line -split '  ', 2
    $file = Join-Path $here ($rel -replace '/', '\')
    if (-not (Test-Path -LiteralPath $file)) { Write-Host "   MISSING $rel"; $bad++; continue }
    if ((Get-FileHash -Algorithm SHA256 -LiteralPath $file).Hash.ToLower() -ne $hash) {
        Write-Host "   BAD     $rel"; $bad++
    }
}
if ($bad) { throw "$bad file(s) failed verification - bundle is damaged or altered" }
Write-Host '   all files OK'

$arch = if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { 'arm64' } else { 'amd64' }
$src = Join-Path $here "bin\windows_$arch\terraform.exe"
if (-not (Test-Path $src)) { throw "no terraform for windows_$arch in this bundle" }
New-Item -ItemType Directory -Force $BinDir | Out-Null
Copy-Item $src (Join-Path $BinDir 'terraform.exe') -Force
Write-Host "== installed $BinDir\terraform.exe"
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if (-not $NoPath -and (($userPath -split ';') -notcontains $BinDir)) {
    [Environment]::SetEnvironmentVariable('Path', ($userPath.TrimEnd(';') + ';' + $BinDir), 'User')
    Write-Host '   added to your user PATH (open a new terminal)'
}
$env:Path = "$BinDir;$env:Path"

# Generate a config pointing at this bundle's real location (forward slashes
# are valid HCL on Windows and avoid escaping). The shipped terraform.rc stays
# untouched so SHA256SUMS.txt keeps verifying on re-runs.
$mirror = (Join-Path $here 'providers') -replace '\\', '/'
$rc = Join-Path $here 'installed.terraform.rc'
$text = (Get-Content (Join-Path $here 'terraform.rc')) -replace '^(    path    = )".*"', ('$1"' + $mirror + '"')
[IO.File]::WriteAllText($rc, (($text -join "`r`n") + "`r`n"), (New-Object Text.UTF8Encoding $false))
if (-not $NoRc) {
    $dest = Join-Path $env:APPDATA 'terraform.rc'
    if ((Test-Path $dest) -and ((Get-FileHash $dest).Hash -ne (Get-FileHash $rc).Hash)) {
        Copy-Item $dest ("$dest.bak." + (Get-Date -Format yyyyMMddHHmmss))
        Write-Host '   backed up existing terraform.rc'
    }
    Copy-Item $rc $dest -Force
    Write-Host "== wrote $dest (mirror: $mirror)"
} else {
    Write-Host "== skipped %APPDATA%\terraform.rc; use:  `$env:TF_CLI_CONFIG_FILE = '$rc'"
}
& (Join-Path $BinDir 'terraform.exe') version
'''


def run_argv(argv, cwd, on_line) -> int:
    """Run argv in cwd, streaming combined output. Used for providers lock."""
    kw = {}
    if sys.platform == "win32":
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                            errors="replace", **kw)
    for line in proc.stdout:
        on_line(_ANSI.sub("", line.rstrip("\n")))
    return proc.wait()


_ANSI = re.compile(r"\x1b\[[0-9;]*m")       # terraform colours its output
