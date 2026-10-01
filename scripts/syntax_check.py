#!/usr/bin/env python3
"""Syntax-check one C++ file with the exact flags it is really built with.

Reads cpp/compile_commands.json rather than guessing, so a pybind11 module gets
the Python headers and the other 21 files do not.

A FILE NOT IN THE DATABASE IS AN ERROR, not a pass. The obvious shape of this
script exits 0 when it cannot find an entry, which means a newly added .cpp --
exactly the file you most want checked -- reports success without being compiled.
It tells you to regenerate instead.
"""
import json
import os
import pathlib
import subprocess
import sys

root = pathlib.Path(__file__).resolve().parent.parent
db_path = root / "cpp" / "compile_commands.json"

if len(sys.argv) < 2:
    sys.exit("usage: syntax_check.py <file.cpp>")
if not db_path.exists():
    sys.exit(f"{db_path.relative_to(root)} missing -- run scripts/gen_compile_commands.sh")

target = pathlib.Path(sys.argv[1]).resolve()
db = json.loads(db_path.read_text())
entry = next((e for e in db if pathlib.Path(e["file"]).resolve() == target), None)

if entry is None:
    if target.suffix in (".hpp", ".h"):
        # Header-only library: headers are compiled through their includers, so
        # there is no entry and nothing to check directly. Say so rather than
        # implying it passed.
        sys.exit(f"{target.name} is a header -- it is checked via the .cpp files "
                 f"that include it. Syntax-check one of those instead.")
    sys.exit(f"{target.name} is not in compile_commands.json. If you just added "
             f"it, run scripts/gen_compile_commands.sh. Not compiling it would "
             f"report success without checking anything.")

args = [a for a in entry["arguments"] if a not in ("-c", "-o", "/dev/null", "-shared")]
args.insert(1, "-fsyntax-only")
sys.exit(subprocess.run(args, cwd=entry["directory"]).returncode)
