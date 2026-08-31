#!/usr/bin/env bash
# Sets up a Python 3.12 virtual environment for epic-hdb using uv, and
# installs the pinned dependency set (Django bumped 5.2.15 -> 6.0.8,
# everything else unchanged) from requirements-py312.txt.
#
# Run this in your actual WSL2 Debian shell, from anywhere -- it cd's to
# wherever this script itself lives (i.e. the project root):
#
#   bash setup_py312_env.sh
#
# What it does:
#   1. Installs uv if it isn't already on PATH (a prebuilt binary
#      installer -- no compilation).
#   2. Has uv fetch a prebuilt Python 3.12 (python-build-standalone
#      project -- also no compilation; see the chat for why this avoids
#      the "compile from source" problem bookworm's apt can't).
#   3. Creates a NEW virtual environment at .venv312 -- deliberately not
#      touching or overwriting whatever venv you use today, so the two
#      can coexist and you can roll back just by not activating this one.
#   4. Installs requirements-py312.txt into it.
#   5. Runs `manage.py check` and the hdb test suite against the new
#      environment, so you know immediately whether anything broke.
#
# Nothing here touches your existing venv, installed packages, or database.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
echo "==> Project root: $SCRIPT_DIR"

# 1. Install uv if missing.
if ! command -v uv >/dev/null 2>&1; then
    echo "==> Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # The installer wires ~/.local/bin into PATH via shell rc files, but
    # THIS script's already-running shell won't pick that up until a new
    # shell starts -- add it explicitly so the rest of this run works.
    export PATH="$HOME/.local/bin:$PATH"
fi
echo "==> uv version: $(uv --version)"

# 2. Fetch a prebuilt Python 3.12 (no compilation).
echo "==> Installing Python 3.12 via uv..."
uv python install 3.12

# 3. Create a fresh venv with that interpreter, under a name that can't
#    collide with an existing one.
echo "==> Creating .venv312..."
uv venv --python 3.12 .venv312

# 4. Install pinned dependencies into it. (uv pip install --python ... is
#    the correct idiom here -- a uv-created venv doesn't necessarily ship
#    pip itself, so this doesn't assume it does.)
echo "==> Installing dependencies (Django 6.0.8, everything else unchanged)..."
uv pip install --python .venv312/bin/python -r requirements-py312.txt

# 5. Sanity-check against the actual project.
echo "==> Python version in new venv:"
.venv312/bin/python --version

echo "==> Django version in new venv:"
.venv312/bin/python -c "import django; print(django.get_version())"

echo "==> Running manage.py check..."
.venv312/bin/python manage.py check

echo "==> Running the hdb test suite (expect 60 tests, all passing)..."
.venv312/bin/python manage.py test hdb

echo ""
echo "==> Done. To start using this environment interactively:"
echo "      source .venv312/bin/activate"
echo ""
echo "    Your existing venv and database are untouched. Once you're happy"
echo "    with this one, rename it to whatever you use day to day and"
echo "    retire the old one -- your call, nothing here does that for you."
