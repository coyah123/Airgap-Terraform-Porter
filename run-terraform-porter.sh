#!/usr/bin/env bash
# Launches the Air-Gapped Terraform Porter on Linux / macOS from a terminal, so
# it inherits this shell's PATH (the terraform / gpg found here are used).
# Make executable once:  chmod +x run-terraform-porter.sh
# On macOS, double-click "Run Terraform Porter.command" instead.
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for py in python3 python; do
    if command -v "$py" >/dev/null 2>&1 && "$py" -c "import tkinter" >/dev/null 2>&1; then
        exec "$py" "$DIR/porter.py" "$@"
    fi
done
echo "Python 3 with tkinter not found." >&2
echo "  Debian/Ubuntu: sudo apt install python3-tk   Fedora: sudo dnf install python3-tkinter" >&2
echo "  macOS: python.org installer, or  brew install python python-tk" >&2
exit 1
