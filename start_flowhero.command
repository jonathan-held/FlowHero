#!/bin/bash
# Start FlowHero on macOS or Linux. The first start creates .venv in this folder
# and downloads the packages into it; nothing is installed elsewhere.
set -eu
cd -- "$(dirname -- "$0")"
if [ ! -x .venv/bin/python ] || ! .venv/bin/python -c 'import flowhero, flowkit, PySide6, pyqtgraph' 2>/dev/null; then
  fh_python=""
  for fh_candidate in python3.12 python3.11 python3.10 python3; do
    if command -v "$fh_candidate" >/dev/null 2>&1 && "$fh_candidate" -c 'import sys; raise SystemExit(not ((3, 10) <= sys.version_info[:2] <= (3, 12)))' 2>/dev/null; then
      fh_python=$fh_candidate
      break
    fi
  done
  if [ -z "$fh_python" ]; then
    echo 'FlowHero needs Python 3.10, 3.11 or 3.12. Install it from https://www.python.org/downloads/ and start this file again.'
    exit 1
  fi
  echo "First start: installing FlowHero's packages into $(pwd)/.venv (needs internet, takes a few minutes)."
  "$fh_python" -m venv .venv
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install -e .
fi
unset PYTHONHOME PYTHONPATH
exec .venv/bin/python -m flowhero "$@"
