#!/usr/bin/env bash
# One-time setup: creates a Python 3.9 virtualenv in .venv and installs deps.
# Usage:  ./setup.sh        then run the bot with:  .venv/bin/python bots.py
set -euo pipefail
cd "$(dirname "$0")"

find_py39() {
  for c in python3.9 /usr/bin/python3 /usr/local/bin/python3.9 /opt/homebrew/bin/python3.9 python3 python; do
    if command -v "$c" >/dev/null 2>&1; then
      if "$c" -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 9) else 1)' 2>/dev/null; then
        command -v "$c"; return 0
      fi
    fi
  done
  # pyenv: install 3.9 if available
  if command -v pyenv >/dev/null 2>&1; then
    pyenv install -s 3.9 >/dev/null 2>&1 || true
    local p; p="$(pyenv prefix 3.9 2>/dev/null || true)"
    [ -n "$p" ] && [ -x "$p/bin/python" ] && { echo "$p/bin/python"; return 0; }
  fi
  return 1
}

PY="$(find_py39)" || {
  echo "ERROR: Python 3.9 not found. Install it, then re-run this script." >&2
  echo "  macOS:  brew install python@3.9   (or: pyenv install 3.9)" >&2
  echo "  Debian: sudo apt install python3.9 python3.9-venv" >&2
  echo "  Windows: use setup.bat" >&2
  exit 1
}
echo "Using $PY ($("$PY" --version))"

"$PY" -m venv .venv
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -r requirements.txt
[ -f config.json ] || cp config.example.json config.json

echo
echo "Done. Edit config.json with your bot tokens, then run:"
echo "  .venv/bin/python bots.py"
