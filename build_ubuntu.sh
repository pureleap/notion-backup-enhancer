#!/usr/bin/env bash
set -euo pipefail

# Builds NotionBackupEnhancer-ubuntu locally with PyInstaller,
# mirroring the CI workflow (.github/workflows/build-and-release-executables.yml).

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

# 1. Require uv (it manages .venv from pyproject.toml)
if ! command -v uv &>/dev/null; then
  echo "Error: uv is required (https://docs.astral.sh/uv/)." >&2
  exit 1
fi

# 2. Run the tests
echo "Running tests..."
uv run pytest -q

# 3. Build (same options as CI)
uv run --with pyinstaller pyinstaller \
  --onefile --console --name NotionBackupEnhancer \
  export-fix/drag_drop_entry.py \
  --distpath export-fix/dist \
  --workpath export-fix/build \
  --specpath export-fix

# 4. Install the artifact next to the script
rm -f "${SCRIPT_DIR}/NotionBackupEnhancer-ubuntu"
mv export-fix/dist/NotionBackupEnhancer "${SCRIPT_DIR}/NotionBackupEnhancer-ubuntu"
chmod +x "${SCRIPT_DIR}/NotionBackupEnhancer-ubuntu"

echo
echo "Built: ${SCRIPT_DIR}/NotionBackupEnhancer-ubuntu"
