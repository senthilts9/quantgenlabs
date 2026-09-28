#!/usr/bin/env bash
# build_bindings.sh — compile the pybind11 extensions into backend/.
#
# The extensions are OPTIONAL. Everything in backend/ imports them defensively and
# falls back to a verified pure-Python path where one exists, so a clone that never
# runs this script still works -- it is just slower, and API responses report
# "impl": "python" so a reader of a number always knows which code produced it.
#
#   ./scripts/build_bindings.sh                      # uses backend/.venv if present
#   PYTHON=python3.12 ./scripts/build_bindings.sh    # or pick your own interpreter
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Prefer the project venv: the extension is ABI-locked to one interpreter version,
# so building against a different python than the one that imports it produces a
# module that exists and cannot be loaded.
if [[ -n "${PYTHON:-}" ]]; then
  PY="$PYTHON"
elif [[ -x "$ROOT/backend/.venv/bin/python" ]]; then
  PY="$ROOT/backend/.venv/bin/python"
else
  PY="python3"
fi

echo "==> interpreter: $PY ($($PY --version))"

if ! $PY -c "import pybind11" 2>/dev/null; then
  echo "    pybind11 not installed for this interpreter."
  echo "    install it with:  $PY -m pip install pybind11"
  echo "    (or: uv pip install --python $PY pybind11)"
  exit 1
fi

INCLUDES=$($PY -m pybind11 --includes)
EXT=$($PY -c "import sysconfig;print(sysconfig.get_config_var('EXT_SUFFIX'))")
CXX="${CXX:-c++}"

# -undefined dynamic_lookup is how a Python extension links on macOS: the
# interpreter's symbols are resolved at load time, not link time.
LDFLAGS=""
if [[ "$(uname -s)" == "Darwin" ]]; then
  LDFLAGS="-undefined dynamic_lookup"
fi

build() {
  local src="$1" mod="$2"
  echo "==> $mod$EXT"
  # shellcheck disable=SC2086
  $CXX -std=c++20 -O3 -Wall -shared -fPIC $LDFLAGS $INCLUDES \
       -I"$ROOT/cpp/include" "$ROOT/cpp/src/$src" \
       -o "$ROOT/backend/$mod$EXT"
}

build mkt_bindings.cpp    mkt_connectivity
build quant_bindings.cpp  quant_engine_cpp
build crypto_bindings.cpp qgl_xrpl_crypto

echo "==> verifying they import"
cd "$ROOT/backend"
$PY - <<'PY'
import importlib
for m in ("mkt_connectivity", "quant_engine_cpp", "qgl_xrpl_crypto"):
    try:
        importlib.import_module(m)
        print(f"    ok   {m}")
    except Exception as e:
        print(f"    FAIL {m}: {e}")
        raise SystemExit(1)
PY
echo "==> done. backend/app/core/connectivity will now report impl=cpp"
