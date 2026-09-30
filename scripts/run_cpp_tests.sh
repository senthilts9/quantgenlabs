#!/usr/bin/env bash
# Build and run every C++ test binary.
#
# Compiles each tests/test_*.cpp directly rather than through CMake: the headers
# are header-only and each test is a single translation unit, so there is nothing
# for a build system to do here, and this runs on a machine with no cmake.
#
# Release flags on purpose. The latency tests measure timing behaviour and the
# lock-free ring's correctness depends on the optimiser not being the only thing
# keeping the ordering honest -- testing a -O0 build would test code that is not
# the code that ships.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-${TMPDIR:-/tmp}}/qgl-cpp-tests"
mkdir -p "$OUT"

CXX="${CXX:-clang++}"
FLAGS=(-std=c++20 -O2 -Wall -Wextra -I"$ROOT/cpp/include" -I"$ROOT/cpp/tests" -pthread)

fail=0; total_bins=0; total_checks=0
for src in "$ROOT"/cpp/tests/test_*.cpp; do
  name="$(basename "$src" .cpp)"
  if ! "$CXX" "${FLAGS[@]}" "$src" -o "$OUT/$name" 2>"$OUT/$name.build.log"; then
    echo "BUILD FAILED  $name"; sed 's/^/    /' "$OUT/$name.build.log" | head -20
    fail=1; continue
  fi
  total_bins=$((total_bins + 1))
  if out="$("$OUT/$name" 2>&1)"; then
    # microtest prints a trailing "N checks" style summary; surface the count so
    # a silently-empty test binary cannot pass for a green one.
    n="$(printf '%s' "$out" | grep -oE '[0-9]+ (checks|assertions|tests)' | tail -1 | grep -oE '^[0-9]+' || true)"
    total_checks=$((total_checks + ${n:-0}))
    printf 'PASS  %-22s %s\n' "$name" "${n:+$n checks}"
  else
    echo "FAIL  $name"; printf '%s\n' "$out" | sed 's/^/    /' | tail -25; fail=1
  fi
done

echo "-----"
echo "$total_bins binaries, $total_checks checks"
exit $fail
