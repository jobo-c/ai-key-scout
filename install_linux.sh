#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

echo "== AI Key Scout v5 - Linux installer =="

if ! command -v python3 >/dev/null 2>&1; then
  echo "[+] Installing Python 3..."
  sudo apt-get update
  sudo apt-get install -y python3
fi

if ! command -v git >/dev/null 2>&1; then
  echo "[+] Installing Git..."
  sudo apt-get update
  sudo apt-get install -y git
fi

if ! command -v cmake >/dev/null 2>&1; then
  echo "[+] Installing CMake and build tools..."
  sudo apt-get update
  sudo apt-get install -y cmake build-essential
else
  echo "[OK] CMake: $(cmake --version | head -n1)"
fi

echo
read -r -p "Create and use a Python virtual environment (.venv)? [Y/n]: " USE_VENV
USE_VENV="${USE_VENV:-Y}"

if [[ "$USE_VENV" =~ ^[Yy]$ ]]; then
  echo "[+] Creating .venv..."
  python3 -m venv .venv
  source .venv/bin/activate
  PYTHON_BIN="python"
else
  echo "[!] Installing into the current Python environment."
  PYTHON_BIN="python3"
fi

echo "[+] Upgrading pip/setuptools/wheel..."
"$PYTHON_BIN" -m pip install --upgrade pip setuptools wheel

echo "[+] Installing runtime dependencies..."
"$PYTHON_BIN" -m pip install PyQt6 requests PyYAML aiohttp

echo "[+] Checking Python compilation..."
"$PYTHON_BIN" -m compileall -q .

echo
echo "[OK] AI Key Scout v5 is installed."
echo "[+] Start with:"
if [[ "$USE_VENV" =~ ^[Yy]$ ]]; then
  echo "    source .venv/bin/activate"
fi
echo "    $PYTHON_BIN main.py"
