#!/usr/bin/env bash
# go_to_bed installer
# Usage: bash install.sh               (installs, then starts the app)
#        GTB_NO_RUN=1 bash install.sh  (installs only)
set -euo pipefail

REPO_URL="https://github.com/ramhee98/go_to_bed"
PROJECT_DIR="go_to_bed"

# 1. Figure out where to install.
#    Running the script from inside an existing clone must not create a nested one.
if [ -f "main.py" ] && [ -f "requirements.txt" ]; then
    echo "==> Existing checkout detected, installing in place: $(pwd)"
elif [ -d "$PROJECT_DIR/.git" ]; then
    echo "==> Existing clone found in ./$PROJECT_DIR, updating"
    cd "$PROJECT_DIR"
    git pull --ff-only
else
    echo "==> Cloning $REPO_URL"
    git clone "$REPO_URL" "$PROJECT_DIR"
    cd "$PROJECT_DIR"
fi

# 2. Check the interpreter. zoneinfo needs 3.9.
PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
    echo "ERROR: $PY not found. Install python3 and python3-venv." >&2
    exit 1
fi
"$PY" - <<'EOF'
import sys
if sys.version_info < (3, 9):
    sys.exit("ERROR: Python 3.9 or newer is required, found %s" % sys.version.split()[0])
EOF
echo "==> Using $("$PY" -V)"

# 3. Virtual environment.
if [ ! -x ".venv/bin/python" ]; then
    echo "==> Creating virtual environment in .venv"
    "$PY" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

# 4. Dependencies.
#    --only-binary=:all: refuses source builds. Without it, a missing wheel for a
#    new Python version silently starts a C/C++ build that can fail hard
#    (for example pandas + Cython on Python 3.13 / GCC 14).
python -m pip install --upgrade pip setuptools wheel
if ! python -m pip install --only-binary=:all: -r requirements.txt; then
    cat >&2 <<'EOF'

ERROR: no prebuilt wheel available for at least one dependency on this
Python version. Options:
  * use a Python version that has wheels (for example 3.11 or 3.12)
  * bump the affected pin in requirements.txt to a release with wheels
  * allow source builds by removing --only-binary=:all: from this script
    (then also install build deps: build-essential python3-dev pkg-config)
EOF
    exit 1
fi

# 5. Config.
if [ ! -f "config.py" ]; then
    cp config.py.template config.py
    echo
    echo "==> Created config.py from the template."
    echo "    Set OURA_TOKEN and your wake times before the first run."
fi

echo
echo "==> Install complete."
echo "    Activate with:   source .venv/bin/activate"
echo "    Generate .ics:   python3 main.py"
echo "    Start the UI:    streamlit run app.py"

# 6. Start the app unless suppressed.
if [ "${GTB_NO_RUN:-0}" != "1" ]; then
    echo
    streamlit run app.py
fi
