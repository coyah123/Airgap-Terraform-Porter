"""
fetch_ops — everything that talks to the internet (online side only), plus
the integrity checks applied to what comes back.

Sources:
  * Terraform CLI  — releases.hashicorp.com
        /terraform/index.json                       (version list)
        /terraform/<v>/terraform_<v>_<os>_<arch>.zip
        /terraform/<v>/terraform_<v>_SHA256SUMS(.sig)
  * Providers      — the Terraform provider registry protocol (v1), discovered
                     per host via https://<host>/.well-known/terraform.json, so
                     any registry works, not just registry.terraform.io:
        <base><ns>/<type>/versions
        <base><ns>/<type>/<v>/download/<os>/<arch>
                     The download response names the zip, its SHA256SUMS, the
                     detached signature and the signing key.

Integrity, in order:
  1. sha256(zip) == the line for that zip in SHA256SUMS (and, for providers,
     == the `shasum` the registry returned).
  2. If gpg is available: SHA256SUMS is verified against its detached .sig.
     The Terraform CLI must be signed by HashiCorp's release key (fingerprint
     pinned below). A provider must be signed by a key the registry lists for
     it, and hashicorp/* providers must be signed by that same HashiCorp key.

Hash formats Terraform's lock file uses, both computed here:
  * zh:<hex>  — sha256 of the .zip (legacy "zip hash").
  * h1:<b64>  — golang dirhash "Hash1" over the zip's contents.

Standard library + an optional external `gpg` binary.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

UA = "airgap-terraform-porter"
RELEASES = "https://releases.hashicorp.com"

# HashiCorp's release-signing key (https://www.hashicorp.com/trust/security).
HASHICORP_FPR = "C874011F0AB405110D02105534365D9472D7468F"
HASHICORP_KEY_URL = "https://www.hashicorp.com/.well-known/pgp-key.txt"


# -- http --------------------------------------------------------------------
# Which certificates HTTPS downloads trust. Verification is NEVER turned off;
# we only add trusted roots that the OS already has:
#   * macOS: the Keychain (system roots + System.keychain + your login keychain),
#     read with Apple's /usr/bin/security. That's what Safari / curl / Terraform
#     trust, including company proxy CAs that IT installs. python.org's macOS
#     Python otherwise trusts nothing until "Install Certificates" is run.
#   * anywhere, if Python's own store is still empty: certifi (if installed) or
#     the OS bundle files below.
#   * SSL_CERT_FILE / SSL_CERT_DIR are honoured (OpenSSL reads them).
_CA_FALLBACKS = (
    "/etc/ssl/cert.pem",                         # macOS system bundle, BSD
    "/opt/homebrew/etc/openssl@3/cert.pem",      # Homebrew (Apple silicon)
    "/usr/local/etc/openssl@3/cert.pem",         # Homebrew (Intel)
    "/etc/ssl/certs/ca-certificates.crt",        # Debian / Ubuntu
    "/etc/pki/tls/certs/ca-bundle.crt",          # RHEL / Fedora
)
_MAC_KEYCHAINS = (
    "/System/Library/Keychains/SystemRootCertificates.keychain",
    "/Library/Keychains/System.keychain",        # MDM / IT-installed company CAs
    "~/Library/Keychains/login.keychain-db",     # CAs you added yourself
)
_ssl_ctx = None
ssl_sources: list[str] = []                      # shown in the startup log / errors


def macos_keychain_pems() -> list[str]:
    """Every certificate in the macOS keychains, as PEM blocks ([] elsewhere)."""
    if sys.platform != "darwin" or not os.path.exists("/usr/bin/security"):
        return []
    pems: list[str] = []
    for kc in _MAC_KEYCHAINS:
        kc = os.path.expanduser(kc)
        if not os.path.exists(kc):
            continue
        try:
            out = subprocess.run(["/usr/bin/security", "find-certificate", "-a", "-p", kc],
                                 capture_output=True, text=True, timeout=20,
                                 stdin=subprocess.DEVNULL).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        pems += split_pems(out)
    return pems


def split_pems(text: str) -> list[str]:
    end = "-----END CERTIFICATE-----"
    out = []
    for chunk in text.split(end):
        i = chunk.find("-----BEGIN CERTIFICATE-----")
        if i >= 0:
            out.append(chunk[i:] + end + "\n")
    return out


def ssl_context() -> ssl.SSLContext:
    global _ssl_ctx
    if _ssl_ctx is None:
        ctx = ssl.create_default_context()
        ssl_sources.clear()
        if sys.platform == "win32":
            ssl_sources.append("Windows certificate store")
        elif _has_default_cafile() or ctx.get_ca_certs():
            ssl_sources.append("Python's CA store")
        pems = macos_keychain_pems()
        ok = 0
        for pem in pems:                         # one at a time: skip any odd cert
            try:
                ctx.load_verify_locations(cadata=pem)
                ok += 1
            except (ssl.SSLError, ValueError):
                pass
        if ok:
            ssl_sources.append(f"macOS Keychain ({ok} certificates)")
        if not ssl_sources:
            try:
                import certifi                   # optional; used only if present
                ctx.load_verify_locations(certifi.where())
                ssl_sources.append("certifi")
            except (ImportError, OSError, ssl.SSLError):
                for f in _CA_FALLBACKS:
                    if os.path.isfile(f):
                        try:
                            ctx.load_verify_locations(f)
                            ssl_sources.append(f)
                            break
                        except (OSError, ssl.SSLError):
                            continue
        _ssl_ctx = ctx
    return _ssl_ctx


def _has_default_cafile() -> bool:
    p = ssl.get_default_verify_paths()
    return bool((p.cafile and os.path.isfile(p.cafile)) or
                (p.capath and os.path.isdir(p.capath) and any(os.scandir(p.capath))))


def presented_issuer(host: str, port: int = 443) -> str:
    """Who issued the certificate this network actually presents for host —
    fetched WITHOUT trusting it, only to explain a failure. '' if unknown."""
    try:
        pem = ssl.get_server_certificate((host, port), timeout=10)
    except TypeError:                            # Python < 3.10: no timeout kwarg
        try:
            pem = ssl.get_server_certificate((host, port))
        except OSError:
            return ""
    except OSError:
        return ""
    try:
        fd, path = tempfile.mkstemp(suffix=".pem")
        with os.fdopen(fd, "w") as fh:
            fh.write(pem)
        info = ssl._ssl._test_decode_cert(path)  # stdlib decoder; display only
        os.unlink(path)
    except Exception:                            # noqa: BLE001
        return ""
    parts = dict(x[0] for x in info.get("issuer", ()))
    return " / ".join(v for v in (parts.get("commonName"), parts.get("organizationName")) if v)


def cert_error_message(url: str, reason) -> str:
    host = urllib.parse.urlparse(url).hostname or url
    issuer = presented_issuer(host)
    trusted = ", ".join(ssl_sources) or "nothing (no CA certificates found)"
    msg = [f"Couldn't verify the HTTPS certificate for {host} ({reason})."]
    if issuer:
        msg.append(f"The certificate this network presents was issued by: {issuer}.")
    msg.append(f"The app trusts: {trusted}.")
    if sys.platform == "darwin" and not any("Keychain" in s for s in ssl_sources):
        msg.append("It couldn't read the macOS Keychain.")
    msg.append("If that issuer is your company (e.g. Zscaler, Netskope, Palo Alto, "
               "Blue Coat, or a corporate CA), a network proxy is inspecting HTTPS: "
               "ask IT to install its root certificate in the macOS System keychain, "
               "or save it as a .pem file and start the app with "
               "SSL_CERT_FILE=/path/to/that.pem. Certificate checks are never "
               "switched off.")
    return " ".join(msg)


def _open(url: str, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        return urllib.request.urlopen(req, timeout=timeout,  # noqa: S310 (https only)
                                      context=ssl_context())
    except urllib.error.URLError as e:
        if isinstance(e.reason, ssl.SSLCertVerificationError):
            raise RuntimeError(cert_error_message(url, e.reason.verify_message
                                                  or e.reason)) from e
        raise RuntimeError(f"{url}: {e.reason}") from e


def get_bytes(url: str) -> bytes:
    with _open(url) as r:
        return r.read()


def get_json(url: str):
    return json.loads(get_bytes(url).decode("utf-8"))


def download_file(url: str, dest: Path, on_line) -> None:
    """Download url -> dest (atomic via .part). Raises on failure."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    try:
        with _open(url, timeout=120) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            done, last = 0, 0.0
            with open(tmp, "wb") as fh:
                while True:
                    chunk = resp.read(262144)
                    if not chunk:
                        break
                    fh.write(chunk)
                    done += len(chunk)
                    now = time.time()
                    if now - last > 0.5:
                        last = now
                        if total:
                            on_line(f"    {done * 100 // total:3d}%  "
                                    f"{done >> 20} / {total >> 20} MB")
                        else:
                            on_line(f"    {done >> 20} MB")
        tmp.replace(dest)
    finally:
        if tmp.exists():
            tmp.unlink()


# -- hashes ------------------------------------------------------------------
def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def h1_of_zip(path: Path) -> str:
    """Terraform's `h1:` package hash (golang.org/x/mod/sumdb/dirhash.Hash1
    applied to the zip's file entries, as Terraform's PackageHashV1 does)."""
    lines = []
    with zipfile.ZipFile(path) as z:
        names = sorted(i.filename for i in z.infolist() if not i.is_dir())
        for name in names:
            if "\n" in name:
                raise ValueError("newline in zip member name")
            lines.append(f"{hashlib.sha256(z.read(name)).hexdigest()}  {name}\n")
    digest = hashlib.sha256("".join(lines).encode("utf-8")).digest()
    return "h1:" + base64.b64encode(digest).decode("ascii")


def parse_shasums(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) == 2:
            out[parts[1].lstrip("*")] = parts[0].lower()
    return out


# -- gpg ---------------------------------------------------------------------
def find_gpg() -> str | None:
    """PATH of the launching terminal, then the macOS system PATH (/etc/paths,
    read directly), then common folders (Git for Windows, Homebrew, GPG Suite)."""
    import tf_locate
    return tf_locate.find_gpg()


def _no_window():
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def _gpg_path(gpg: str, path) -> str:
    """Path as this gpg understands it. Git for Windows ships an msys gpg that
    only resolves POSIX-style paths (/c/Users/...), not C:/Users/... ."""
    s = str(path)
    if sys.platform == "win32" and (Path(gpg).parent / "msys-2.0.dll").exists():
        s = s.replace("\\", "/")
        if len(s) > 1 and s[1] == ":":
            s = "/" + s[0].lower() + s[2:]
    return s


def gpg_verify(gpg: str, armored_keys: list[str], data: Path, sig: Path) -> str:
    """Verify `sig` over `data` using ONLY the given keys, in a throwaway
    keyring. Returns the primary-key fingerprint that made a valid signature.
    Raises RuntimeError on any failure."""
    home = tempfile.mkdtemp(prefix="tfporter-gpg-")
    try:
        base = [gpg, "--homedir", _gpg_path(gpg, home), "--batch", "--no-tty", "--quiet"]
        for key in armored_keys:
            r = subprocess.run(base + ["--import"], input=key.encode("utf-8"),
                               capture_output=True, **_no_window())
            if r.returncode != 0:
                raise RuntimeError("gpg --import failed: "
                                   + r.stderr.decode("utf-8", "replace").strip())
        r = subprocess.run(base + ["--status-fd", "1", "--verify",
                                   _gpg_path(gpg, sig), _gpg_path(gpg, data)],
                           capture_output=True, **_no_window())
        status = r.stdout.decode("utf-8", "replace")
        for line in status.splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[0] == "[GNUPG:]" and parts[1] == "VALIDSIG":
                return parts[-1].upper()      # primary key fingerprint
        raise RuntimeError("signature did not verify: "
                           + (r.stderr.decode("utf-8", "replace").strip() or status.strip()))
    finally:
        shutil.rmtree(home, ignore_errors=True)


_hashicorp_key_cache: str | None = None


def hashicorp_key() -> str:
    global _hashicorp_key_cache
    if _hashicorp_key_cache is None:
        _hashicorp_key_cache = get_bytes(HASHICORP_KEY_URL).decode("utf-8")
    return _hashicorp_key_cache


# -- Terraform CLI releases --------------------------------------------------
_tf_index_cache = None


def terraform_versions() -> list[str]:
    global _tf_index_cache
    if _tf_index_cache is None:
        _tf_index_cache = get_json(f"{RELEASES}/terraform/index.json")
    return list(_tf_index_cache.get("versions", {}).keys())


def terraform_builds(version: str) -> set[str]:
    terraform_versions()
    v = _tf_index_cache["versions"].get(version) or {}
    return {f"{b['os']}_{b['arch']}" for b in v.get("builds", [])}


def terraform_urls(version: str, platform: str) -> dict:
    base = f"{RELEASES}/terraform/{version}"
    zip_name = f"terraform_{version}_{platform}.zip"
    sums = f"terraform_{version}_SHA256SUMS"
    return {"zip_name": zip_name, "zip": f"{base}/{zip_name}",
            "sums_name": sums, "sums": f"{base}/{sums}",
            "sig_name": sums + ".sig", "sig": f"{base}/{sums}.sig"}


# -- provider registry -------------------------------------------------------
_discovery: dict[str, str] = {}


def providers_base(host: str) -> str:
    """Service discovery: https://<host>/.well-known/terraform.json."""
    if host not in _discovery:
        url = f"https://{host}/.well-known/terraform.json"
        doc = get_json(url)
        rel = doc.get("providers.v1")
        if not rel:
            raise RuntimeError(f"{host} does not offer a provider registry")
        _discovery[host] = urllib.parse.urljoin(url, rel)
    return _discovery[host]


_versions_cache: dict[str, list[dict]] = {}


def provider_versions(address: str) -> list[dict]:
    """[{version, protocols, platforms:[{os,arch}]}] for host/ns/type."""
    if address not in _versions_cache:
        host, ns, typ = address.split("/")
        _versions_cache[address] = get_json(
            f"{providers_base(host)}{ns}/{typ}/versions").get("versions", [])
    return _versions_cache[address]


def provider_platforms(address: str, version: str) -> set[str]:
    for v in provider_versions(address):
        if v.get("version") == version:
            return {f"{p['os']}_{p['arch']}" for p in v.get("platforms", [])}
    return set()


def provider_download(address: str, version: str, platform: str) -> dict:
    host, ns, typ = address.split("/")
    os_, arch = platform.split("_", 1)
    url = f"{providers_base(host)}{ns}/{typ}/{version}/download/{os_}/{arch}"
    doc = get_json(url)
    for k in ("download_url", "shasums_url", "shasums_signature_url"):
        if doc.get(k):
            doc[k] = urllib.parse.urljoin(url, doc[k])
    return doc


@dataclass
class Fetched:
    path: Path
    sha256: str
    sums_ok: bool
    gpg_fpr: str | None     # primary-key fingerprint that signed SHA256SUMS
    sums_path: Path
    sig_path: Path | None
