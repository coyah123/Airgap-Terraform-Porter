#!/bin/bash
# macOS launcher: double-click in Finder. It opens in Terminal, so the app
# inherits your shell's PATH — the `terraform` and `gpg` you use there are the
# ones the app uses. (First time only, if macOS says it can't be opened:
#   chmod +x "Run Terraform Porter.command"   then right-click > Open.)
cd "$(dirname "$0")" || exit 1
for py in python3 python; do
  if command -v "$py" >/dev/null 2>&1 && "$py" -c "import tkinter" >/dev/null 2>&1; then
    exec "$py" porter.py
  fi
done
echo "Python 3 with tkinter not found."
echo "Install Python from https://www.python.org/downloads/macos/ (includes Tk),"
echo "or with Homebrew:  brew install python python-tk"
read -r -p "Press Return to close."
