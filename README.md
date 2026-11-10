# 🏗 Air-Gapped Terraform Porter

A native desktop tool that scans a folder of Terraform code on an
internet-connected machine, works out exactly which **Terraform CLI** and
**providers** it needs, downloads and verifies them for **every target
platform**, and writes a bundle to carry across an air gap. On the far side,
`terraform init` installs everything from that bundle with **no network and no
change to any `.tf` file**.

Sibling to the Air-Gapped Image Porter and Python Porter, with the same design:
standard-library tkinter only (nothing to `pip install`), every action staged as
a plan you review before it runs, and work kept off the UI thread.

## Requirements
Windows, macOS or Linux.
- **Python 3.10+ with tkinter.** Internet on the machine you build on.
  - Windows: python.org / Microsoft Store Python includes tkinter.
  - **macOS:** use the python.org installer, which includes Tk, or Homebrew:
    `brew install python python-tk`. Apple's `/usr/bin/python3` ships an old Tk;
    avoid it.
  - Linux: `sudo apt install python3-tk` (Debian/Ubuntu) or
    `sudo dnf install python3-tkinter` (Fedora).
- Optional: `gpg` for signature checks. It's found on PATH, in Git for Windows,
  Homebrew (`/opt/homebrew/bin`) or GPG Suite.
- Optional: `terraform`, used by the Plan Explorer and to update lock files. See
  *Which terraform the app uses* below. Without one, the bundle's own binary for
  this machine is used.
- The air-gapped side needs nothing but the bundle.

## Run
| OS | Start it |
|---|---|
| Windows | double-click **`Run Terraform Porter.bat`**, or `python porter.py` |
| **macOS** | double-click **`Run Terraform Porter.command`**. It opens in Terminal, so the app gets your shell's PATH. First time only, if macOS refuses: `chmod +x "Run Terraform Porter.command"`, then right-click → Open. Or `python3 porter.py` |
| Linux | `./run-terraform-porter.sh`, or `python3 porter.py` |

```
python porter.py --cli --scan ../my-terraform --out my-terraform-bundle
python porter.py --cli --help     # headless: --platforms, --tf-version, --terraform, --no-lock, --plan-only …
```

Files copied from Windows (zip, USB) lose the Unix "executable" flag. Run the `chmod +x`
above once on macOS or Linux.

### Which terraform the app uses
The app uses the `terraform` **on the PATH of the terminal you start it from**,
so you get the same binary and version as typing `terraform` in that terminal.
One search order is used everywhere (Plan Explorer, the lock-file step, startup
log):

1. **Your choice.** The Plan Explorer's Terraform dropdown, Browse…, or `--terraform` on the CLI.
2. **The launching terminal's PATH.**
3. **Your login shell's PATH** (macOS/Linux, when 2 found nothing). Apps started
   from Finder or a desktop icon get a minimal PATH without Homebrew, tfenv or
   asdf. This brings them back.
4. **Common install folders:** Homebrew, `/usr/local/bin`, `~/.local/bin`,
   tfenv, WinGet, Chocolatey and Scoop.
5. **The bundle's binary**, `bin/<os>_<arch>/terraform`, for this machine.

Under the Terraform field, the Plan Explorer shows what it picked and why, e.g.
`terraform 1.16.5 — from your terminal's PATH ✓ meets >= 1.10`. It turns red if
the version doesn't satisfy the stack's `required_version`. Everything else it
found is in the dropdown, and ⟳ re-scans. The Output log lists the same at
startup.

### macOS notes
- Scrolling the map works with a mouse wheel or trackpad; Shift scrolls sideways.
- Plans and Terraform's working data go to `~/Library/Caches/airgap-terraform-porter`.
  That's `%LOCALAPPDATA%` on Windows and `~/.cache` on Linux.
- To run the **bundle** on a Mac, build it with `darwin_arm64` (Apple Silicon)
  and/or `darwin_amd64` (Intel) ticked on the Platforms screen. The app itself
  runs either way.

## Workflow (GUI)
1. **Scan Project**: pick the folder holding your Terraform code, e.g. `../my-terraform`, and
   press Scan. It lists every provider (source, constraints, locked versions), every
   Terraform directory (root or child module), the `required_version`, and
   warnings, such as a module whose `source` is a registry or git URL and so
   isn't vendored.
2. **Platforms**: `linux_amd64`, `linux_arm64` and `windows_amd64` are ticked by
   default. `darwin_arm64`, `darwin_amd64` and `windows_arm64` are optional extras,
   and you can add any `os_arch`.
3. **Build Bundle**: choose the bundle folder and the CLI version (blank means the
   newest release matching `required_version`). Signature checks and lock-file
   updates are on by default. **Stage** resolves exact versions and lists every
   download in Preview / Run.
4. **Preview / Run**: review, then **Run plan**.
5. **Files**: browse the bundle, then copy it to transport media.

## Plan Explorer (RUN, either side)
A GUI over `terraform init → validate → plan` for one stack. It shows whether the
stack **passes**, and **draws what the plan would do** as a tree, much like Argo
CD's UI draws an application's resources.

Init, validate and plan are **read-only**: a plan *reads* the target account
(refresh and data sources) but creates, changes and destroys nothing. **Apply**
is a separate, guarded button (see *Apply* below).

- **Pick a stack.** Choose from the scanned roots, or Browse. Add var files; an
  `environments/*.tfvars` file is pre-filled. Set the AWS profile if needed; it's
  passed as `AWS_PROFILE`, and the list comes from `~/.aws/config`.
- **Providers from bundle (offline)** runs `init -plugin-dir=<bundle>/providers`
  and uses the bundle's own terraform binary. Nothing touches a registry, so this
  is the mode for the air-gapped side. A bundle folder next to the stack, or one
  level up, is detected automatically by its contents (`providers/` plus the
  app's `MANIFEST.json`), whatever it's called.
- **Local backend, no state** writes a temporary, clearly marked
  `backend_override.tf`. Terraform's state and provider cache go to a private
  `TF_DATA_DIR` in the app's cache folder (see *macOS notes*). The override is
  removed afterwards, even on failure, and the app refuses to overwrite a
  `backend_override.tf` it didn't write. With no state, everything plans as
  *create*. Untick it to plan against the real backend; use `-backend-config`
  for an `environments/*.tfbackend` file.
- **Pass/fail.** Three badges (✓/✗ init · validate · plan) and Terraform's summary
  line. Errors and warnings appear on the **Problems** tab as `file:line`, and
  selecting one shows Terraform's full message.
- **Map.** Boxes run left to right: **stack → modules → resources**. `count` and
  `for_each` instances are grouped under one box, e.g. `aws_subnet cluster ×3`.
  Each box is coloured by its planned action: create, update, replace, destroy,
  read, or no change. Modules and groups take the most severe action inside
  them.
- **Click a box** to see its details: address, action, Terraform's reason (e.g.
  *delete because no module*), what forces a replacement, which resources it
  references, and an attribute **before → after** diff. Values only known after
  apply are marked, and sensitive values are masked.
- **List view** shows the same tree as an indented table. **Unchanged** and **data
  sources** filters hide noise.
- **Open plan file…** views a saved plan without running anything: a
  `terraform show -json` file, or a binary `.tfplan` from an initialised
  directory. **Save plan JSON…** exports the current plan, e.g. to review it on
  another machine.

### Apply
**⚠ Apply this plan…** changes real infrastructure, so it is fenced in:

- **It applies exactly the plan you're looking at.** It runs
  `terraform apply <saved plan file>`, never a fresh plan with `-auto-approve`. If
  the state moved on since the plan, Terraform itself refuses ("saved plan is
  stale"). The plan file is single-use and deleted after apply.
- **The button stays disabled, with the reason shown next to it, when:**
  - there's no plan from this session (plans opened from a file are view-only);
  - the plan errored, or has no changes;
  - **the plan used "local backend, no state" on a stack that has a real
    backend.** Applying it would create real resources with throwaway state. Untick
    it, plan against the real backend (`-backend-config=…`), then apply. Stacks with
    no backend block, such as a bootstrap stack that creates the state bucket, are
    fine, because their local state file *is* their real state;
  - **any `.tf`, lock or var file changed after the plan.** Re-plan first.
- **Confirmation dialog.** It shows the stack, plan summary, AWS account(s) (from
  `allowed_account_ids`), profile, var files and where the state lives. It lists
  every resource that will be **destroyed or replaced**, in red. The Apply button
  only enables once you **type the stack folder name**.
- **Live progress.** Boxes get a blue outline while being applied, then green ✓
  or red ✗; modules and groups roll up their children. The **apply** badge and
  summary show `N added, N changed, N destroyed`. Errors go to Problems, and on a
  failure you're told to re-plan to see where things stand.

After an apply, press **▶ Init + Validate + Plan** again. It should show
`0 to add, 0 to change, 0 to destroy`, and Apply disables itself ("nothing to
apply").

Tested end to end on a scratch copy of the local-only `terraform_data` fixture.
There, apply created 8, the re-plan showed no changes, and a second plan
(2 add, 1 change, 2 destroy) listed the two destroys in the dialog and applied
cleanly. Editing `main.tf` after the plan disabled Apply. No AWS apply was run.

Also tested with a read-only plan of a real AWS stack, using the bundle's
providers and a local backend. A deliberately broken copy of the fixture was
flagged at `main.tf:18` ("Reference to undeclared resource").

Note: a profile that uses the newer `aws login` session (`login_session = …`)
can't be read by the 5.x AWS provider. Add a second profile that wraps it, and
pick that one in the Plan Explorer:
```ini
[profile my-profile-tf]
credential_process = aws configure export-credentials --profile my-profile --format process
region = us-east-1
```

## What gets resolved
- **Providers**: every `required_providers` entry in every `.tf` / `.tf.json`,
  plus implicit ones (a `provider "x"` block or `x_*` resource with no entry,
  which Terraform treats as `hashicorp/x`). This is generic: aws, tls, time,
  null, cloudinit, and providers from any registry host (found through
  `/.well-known/terraform.json`).
- **Versions**: a root's `.terraform.lock.hcl` pins the exact version, and that
  version is mirrored. If any root has no lock entry, the newest release that
  meets **every** module's constraints is mirrored as well.
- **Terraform core**: the newest release meeting every `required_version`, unless
  you pin one.

## The bundle
```
<bundle>/
  bin/<os>_<arch>/terraform[.exe]           extracted CLI, ready to run
  terraform/terraform_<v>_<os>_<arch>.zip   original release zips
  providers/<host>/<ns>/<type>/
      terraform-provider-<type>_<v>_<os>_<arch>.zip
      index.json   <v>.json                 ← same layout `terraform providers mirror` writes
  checksums/                                upstream SHA256SUMS, .sig files, HashiCorp key
  terraform.rc   .terraformrc               mirror-only CLI configs (Windows / Linux)
  install.ps1    install.sh                 verify + install on the far side
  INSTALL.md     MANIFEST.json   SHA256SUMS.txt
```
As a size guide: Terraform plus `hashicorp/aws` for three platforms comes to
about **900 MB**, mostly the AWS provider at about 150 MB per platform.

The CLI configs contain:
```hcl
provider_installation {
  filesystem_mirror {
    path    = "/opt/terraform-airgap/providers"   # install scripts rewrite this
    include = ["*/*/*"]
  }
  direct {
    exclude = ["*/*/*"]                          # never contact any registry
  }
}
disable_checkpoint = true
```

## Install on the air-gapped side
```bash
bash install.sh             # Linux/macOS: verifies SHA256SUMS.txt, installs terraform to
                            # ~/.local/bin (or /usr/local/bin as root), writes ~/.terraformrc
```
```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1   # Windows: verifies, installs to
   # %LOCALAPPDATA%\terraform-airgap\bin (+ user PATH), writes %APPDATA%\terraform.rc
```
Both scripts write `installed.terraformrc` / `installed.terraform.rc`, with the
mirror `path` pointing at wherever the bundle actually sits. Use `--no-rc` /
`-NoRc` to leave your existing config alone and set `TF_CLI_CONFIG_FILE` instead.
`-NoPath` and `-BinDir` are also available. The installers run on their own, with
no Python needed. `INSTALL.md` in the bundle holds the same instructions.

## Your `.tf` files don't change
`source = "hashicorp/aws"` stays exactly as it is. The `source` is a provider
**address** (`registry.terraform.io/hashicorp/aws`), not a download location.
`provider_installation` in the CLI config decides where an address is installed
from. With `filesystem_mirror` plus `direct { exclude = ["*/*/*"] }`, every
address is served from `providers/<host>/<ns>/<type>/`, and Terraform has no
installation method that could reach the internet. This was tested end to end
(see Verification): an unmodified stack ran `terraform init` with all network
access blocked.

## Design choice: direct registry download vs. wrapping `terraform providers mirror`
`terraform providers mirror -platform=…` is the standard way, and it's what
HashiCorp's docs and most guides use. This tool downloads from the registry API
directly instead, but **writes the same output**. The `providers/` folder holds the
same zip names and the same `index.json` / `<version>.json` (byte-identical for
index.json; same archives and h1 hashes for the version file, checked against a
real `terraform providers mirror` run). So any procedure written for a
`providers mirror` directory works on this bundle unchanged: tar it up, extract
to `/opt/...`, point `filesystem_mirror` at it, or serve it as a `network_mirror`.

Why download directly:
- **One pass for many roots.** `providers mirror` runs per root directory and
  re-downloads shared providers each time. Several root modules × three platforms
  × a 150 MB aws provider adds up fast. Here, each (provider, version, platform) is fetched
  once.
- **No Terraform needed to build, and no config quirks.** It doesn't need a working
  `terraform` binary of the right version, and it isn't tripped up by backends or
  modules that `providers mirror` would have to load.
- **Verification you can show.** Each zip is checked against the upstream
  SHA256SUMS and the registry's `shasum`. The SHA256SUMS file's GPG signature is
  checked against HashiCorp's key, pinned by fingerprint
  `C874 011F 0AB4 0511 0D02 1055 3436 5D94 72D7 468F`. The SHA256SUMS files,
  signatures and key are shipped in `checksums/`, so the air-gapped side can
  re-check them.
- **Lock hashes computed up front.** `h1:` and `zh:` are recorded in MANIFEST.json.
  The h1 computed for a zip matches the one Terraform itself writes to lock
  files (checked against an existing lock).

`terraform providers mirror` remains a fine manual fallback. Its output can be
dropped into `providers/` as-is.

## Lock files (`.terraform.lock.hcl`)
With "update lock files" on (the default), each root gets
`terraform providers lock -platform=<each> -fs-mirror=<bundle>/providers`. This
adds `h1:` hashes for every platform offline, from the bundle itself, and keeps
the existing `zh:` hashes. Afterwards the tool checks that every root's lock
covers every bundled platform. Choose the "registry" hash source to fetch from
the registry instead, which is slower and re-downloads every platform.

What actually happens without it (tested on linux_arm64 with the original,
Windows-made lock): init **still succeeds** from this packed mirror, because the
lock's `zh:` hashes cover all platforms. But Terraform **rewrites the lock** to
add the new platform's `h1:`, so every Linux runner dirties the file. In CI with
`-lockfile=readonly`, or with an unpacked mirror or plugin cache (where only
`h1:` can be checked), it **fails**. Updating the locks makes them stable on every
platform. Commit the updated lock files.

## How it was verified
Against a real multi-root AWS repo (six root modules, `hashicorp/aws` 5.100.0):
- Bundle built for linux_amd64, linux_arm64 and windows_amd64. GPG and SHA256
  checks passed for Terraform and the AWS provider, and every root's lock file
  was updated to cover all 3 platforms.
- **Windows offline init:** `install.ps1 -NoRc -NoPath` verified all checksums.
  In one stack, a temporary `backend_override.tf` set a local backend, and
  `TF_CLI_CONFIG_FILE` pointed at the bundle config, with `HTTP(S)_PROXY` set to a
  dead port so any network attempt would fail. `terraform init` installed the
  provider from the bundle and `terraform validate` passed. The lock file was
  unchanged, the override was deleted afterwards, and `.terraform` went to a
  scratch `TF_DATA_DIR`. Nothing was applied.
- **Control:** the same dead proxy without the bundle config makes init fail
  (`could not connect to registry.terraform.io`), so the network really was
  blocked.
- **Linux arm64 offline init (on an ARM64 Linux machine):** a copy made on Windows, so it lost
  the exec bit. `install.sh --no-rc` verified it and installed it, and init
  succeeded with the lock unchanged.

Note: a `backend_override.tf` has to be multi-line. `terraform { backend "local" {} }`
on one line is rejected, because a single-line block can't contain a nested block:
```hcl
terraform {
  backend "local" {}
}
```

## Files
- `porter.py`: the tkinter GUI and the `--cli` headless mode, with JobRunner.
- `scan_ops.py`: finds roots and modules, providers, constraints and lock entries.
- `hcl_lite.py`: a small HCL2 tokenizer/parser, enough for Terraform's structural blocks.
- `tf_versions.py`: Terraform version-constraint semantics (`~>`, ranges, pre-releases).
- `fetch_ops.py`: releases and registry APIs, downloads, SHA256SUMS, GPG, h1/zh hashes.
- `bundle_ops.py`: the plan and steps, mirror index, lock update/check, CLI configs,
  install scripts, manifest.
- `plan_ops.py`: Plan Explorer engine. Runs init/validate/plan/show/apply (`-json`), the apply guards,
  manages the temporary local backend, turns plan JSON into a tree, and builds
  attribute diffs.
- `plan_view.py`: the Plan Explorer screen (map/list, details, problems).
- `tf_locate.py`: finds `terraform` / `gpg` (terminal PATH → login shell → common folders → bundle).
- `Run Terraform Porter.bat` / `.command` / `run-terraform-porter.sh`: Windows / macOS / Linux launchers.
- `tests/`: `python -m unittest discover -s tests`. These run offline. They include
  a multi-provider fixture (tls/time/null/cloudinit, an implied provider, a
  vendored module, a .tf.json file) and `fixtures/plan_stack`. That is a small
  stack using only the built-in `terraform_data`, which needs no providers or
  network. Its real create and update/destroy plans are saved as JSON.
