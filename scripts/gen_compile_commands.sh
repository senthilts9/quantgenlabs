#!/usr/bin/env bash
# Generate cpp/compile_commands.json for IntelliSense (and clangd, if you prefer it).
#
# WHY A GENERATOR rather than a hand-written includePath. Three of the sources
# are pybind11 modules and need the Python and pybind11 headers; the other
# twenty-one must NOT see them. A single includePath that covers everything
# makes IntelliSense offer Python C-API completions inside the hot-path order
# book, and hides the real error when a binding forgets its include. A
# compilation database gives each file exactly the flags it is really built with.
#
# The Python paths are resolved HERE, at run time, from the project venv --
# which is why this is generated and gitignored rather than committed. They are
# machine-specific (a uv-managed interpreter on this box), and a committed
# absolute path would be wrong on every other machine including CI.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$ROOT/cpp/compile_commands.json"

PY="$ROOT/backend/.venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3)"

PY_INC=""; PB_INC=""
if [[ -x "$PY" ]]; then
  PY_INC="$("$PY" -c 'import sysconfig;print(sysconfig.get_paths()["include"])' 2>/dev/null || true)"
  PB_INC="$("$PY" -c 'import pybind11;print(pybind11.get_include())' 2>/dev/null || true)"
fi
[[ -n "$PB_INC" ]] || echo "note: pybind11 not importable from $PY -- binding files will show unresolved <pybind11/*> includes" >&2

CXX="${CXX:-$(command -v clang++ || command -v g++)}"
SDK="$(xcrun --show-sdk-path 2>/dev/null || true)"

ROOT="$ROOT" OUT="$OUT" CXX="$CXX" PY_INC="$PY_INC" PB_INC="$PB_INC" SDK="$SDK" \
python3 - <<'PY'
import json, os, pathlib, re

root = pathlib.Path(os.environ["ROOT"])
cxx, py_inc, pb_inc, sdk = (os.environ[k] for k in ("CXX", "PY_INC", "PB_INC", "SDK"))

# Matches the real build: scripts/run_cpp_tests.sh and deploy/Dockerfile.api both
# use -std=c++20 -O2/-O3 with the same include roots. IntelliSense should see the
# flags the code is actually compiled with, not a tidier set.
base = ["-std=c++20", "-Wall", "-Wextra", "-pthread",
        f"-I{root/'cpp'/'include'}", f"-I{root/'cpp'/'tests'}"]
if sdk:
    base += ["-isysroot", sdk]

needs_pybind = re.compile(r'^\s*#\s*include\s*[<"]pybind11/', re.M)

entries = []
for src in sorted(list((root/"cpp"/"tests").glob("*.cpp")) + list((root/"cpp"/"src").glob("*.cpp"))):
    args = [cxx] + list(base)
    if needs_pybind.search(src.read_text(errors="ignore")):
        if py_inc: args.append(f"-I{py_inc}")
        if pb_inc: args.append(f"-I{pb_inc}")
        args += ["-fPIC", "-shared"]
    args += ["-c", str(src), "-o", f"/dev/null"]
    entries.append({"directory": str(root), "arguments": args, "file": str(src)})

out = pathlib.Path(os.environ["OUT"])
out.write_text(json.dumps(entries, indent=2) + "\n")
pb = sum(1 for e in entries if any("pybind11" in a for a in e["arguments"]))
print(f"wrote {out.relative_to(root)}: {len(entries)} files ({pb} with pybind11, {len(entries)-pb} without)")
PY
