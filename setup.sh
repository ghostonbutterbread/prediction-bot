#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${ROOT_DIR}/.venv"
PYTHON_BIN="${PYTHON:-python3}"

echo "[setup] project root: ${ROOT_DIR}"
echo "[setup] python: ${PYTHON_BIN}"

if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  echo "[setup] error: ${PYTHON_BIN} not found" >&2
  exit 1
fi

if [ ! -d "${VENV_DIR}" ]; then
  echo "[setup] creating virtual environment at ${VENV_DIR}"
  "${PYTHON_BIN}" -m venv "${VENV_DIR}"
else
  echo "[setup] reusing existing virtual environment at ${VENV_DIR}"
fi

# shellcheck disable=SC1091
source "${VENV_DIR}/bin/activate"

python -m pip install --upgrade pip setuptools wheel
python -m pip install -r "${ROOT_DIR}/requirements.txt"

echo
cat <<'EOF'
[setup] done.

Next steps:
  source .venv/bin/activate
  cp .env.example .env   # if needed
  python main.py paper
EOF
