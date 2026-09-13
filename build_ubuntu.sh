#!/usr/bin/env bash
set -euo pipefail

# Builds NotionBackupEnhancer-ubuntu locally with PyInstaller,
# mirroring the CI workflow (.github/workflows/build-and-release-executables.yml).

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}/export-fix"

VENVS_DIR="${SCRIPT_DIR}/.build-venvs"
mkdir -p "${VENVS_DIR}"
VENV_DIR="${VENVS_DIR}/ubuntu"
trap 'rm -rf "${VENVS_DIR}" "${SCRIPT_DIR}/export-fix/build"' EXIT

# 1. Set up a Python environment
if command -v uv &>/dev/null; then
  if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
    echo "Creating virtualenv with uv..."
    uv venv --quiet "${VENV_DIR}"
  fi
  uv pip install --quiet --python "${VENV_DIR}/bin/python" pyinstaller
  PY="${VENV_DIR}/bin/python"
  PYINSTALLER="${VENV_DIR}/bin/pyinstaller"
elif command -v python3 &>/dev/null; then
  if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
    echo "Creating virtualenv with python3 -m venv..."
    python3 -m venv "${VENV_DIR}" || {
      echo "Error: cannot create a virtualenv; install uv or python3-venv." >&2
      exit 1
    }
  fi
  PY="${VENV_DIR}/bin/python"
  "${PY}" -m pip install --quiet --upgrade pip
  "${PY}" -m pip install --quiet pyinstaller
  PYINSTALLER="${VENV_DIR}/bin/pyinstaller"
else
  echo "Error: no python3 or uv found." >&2
  exit 1
fi

# 2. Run the tests if pytest is available
if "${PY}" -c "import pytest" &>/dev/null; then
  echo "Running tests..."
  "${PY}" -m pytest tests/ -q
else
  echo "pytest not available; skipping tests"
fi

# 3. Build (same options as CI)
"${PYINSTALLER}" --onefile --console --name NotionBackupEnhancer drag_drop_entry.py

# 4. Install the artifact next to the script
rm -f "${SCRIPT_DIR}/NotionBackupEnhancer-ubuntu"
mv dist/NotionBackupEnhancer "${SCRIPT_DIR}/NotionBackupEnhancer-ubuntu"
chmod +x "${SCRIPT_DIR}/NotionBackupEnhancer-ubuntu"

echo
echo "Built: ${SCRIPT_DIR}/NotionBackupEnhancer-ubuntu"
