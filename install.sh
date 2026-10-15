#!/usr/bin/env bash
# Slurrymix setup: installs project-level dependencies, verifies the import and
# exits. It never starts the tool and never installs system packages - see
# INSTALL.md for the system prerequisites.
#
#   bash install.sh
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "error: $PY not found - install Python 3.10 or newer (see INSTALL.md)" >&2
  exit 1
fi

echo "==> upgrading pip, setuptools and wheel"
if ! "$PY" -m pip install --upgrade pip setuptools wheel; then
  echo "warning: could not reach the package index to upgrade pip - continuing with what is installed" >&2
fi

echo "==> installing project dependencies (requirements.txt)"
"$PY" -m pip install -r requirements.txt

echo "==> installing the slurrymix console command (editable, no build isolation)"
"$PY" -m pip install -e . --no-build-isolation

echo "==> verifying that the module imports"
"$PY" -c "import slurrymix; print('slurrymix', slurrymix.VERSION, 'ready')"

cat <<'TEXT'

Setup complete. Nothing is running in the background and no state file was created.
Try it:
  python3 slurrymix.py --help      (or: slurrymix --help)
  python3 slurrymix.py status
  bash demo.sh                     end-to-end demo on throwaway data
  python3 -m unittest -v           regression suite (or: python3 -m pytest)
TEXT
