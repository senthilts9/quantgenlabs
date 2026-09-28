#!/usr/bin/env bash
# run.sh — reproduce every benchmark number the Performance Lab displays.
#
# Builds the harness twice from identical source, once without SIMD flags and
# once with AVX2+FMA, and writes both result files. The two-build comparison is
# the point: it isolates the contribution of vectorisation on THIS host rather
# than asserting a speedup from a different machine.
#
#   ./benchmarks/run.sh                # 20M paths, 5 repetitions
#   ./benchmarks/run.sh 50000000 7     # more paths, more repetitions
#
# Output: benchmarks/results-baseline.json and benchmarks/results-avx2.json
set -euo pipefail

PATHS="${1:-20000000}"
REPS="${2:-5}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$ROOT/benchmarks"
CXX="${CXX:-g++}"

COMMON=(-std=c++20 -O3 -Wall -Wextra -pthread -I"$ROOT/cpp/include")

echo "==> compiler: $($CXX --version | head -1)"
echo "==> paths=$PATHS reps=$REPS"

# On Linux the harness cannot determine the physical core count without parsing
# /proc, and a wrong physical-core count makes every scaling-efficiency figure
# wrong. So it is passed in explicitly rather than guessed.
if [[ "$(uname -s)" == "Linux" ]]; then
  export QGL_PHYSICAL_CORES="${QGL_PHYSICAL_CORES:-$(lscpu -p=Core,Socket | grep -v '^#' | sort -u | wc -l)}"
  echo "==> QGL_PHYSICAL_CORES=$QGL_PHYSICAL_CORES"
fi

echo "==> building baseline (no SIMD flags)"
$CXX "${COMMON[@]}" "$ROOT/cpp/src/mkt_bench.cpp" -o /tmp/qgl_bench_baseline

echo "==> building AVX2+FMA"
if $CXX "${COMMON[@]}" -mavx2 -mfma "$ROOT/cpp/src/mkt_bench.cpp" -o /tmp/qgl_bench_avx2 2>/dev/null; then
  HAVE_AVX2=1
else
  # A non-x86 host (Apple Silicon, ARM servers) has no AVX2 at all. Skipping is
  # correct; emitting a baseline result labelled AVX2 would not be.
  echo "    -mavx2 unsupported by this toolchain/arch -- skipping the AVX2 variant"
  HAVE_AVX2=0
fi

echo "==> running baseline"
/tmp/qgl_bench_baseline "$PATHS" "$REPS" > "$OUT/results-baseline.json"

if [[ "$HAVE_AVX2" == "1" ]]; then
  echo "==> running AVX2+FMA"
  /tmp/qgl_bench_avx2 "$PATHS" "$REPS" > "$OUT/results-avx2.json"
else
  rm -f "$OUT/results-avx2.json"
fi

echo "==> done:"
ls -la "$OUT"/results-*.json
python3 - "$OUT" <<'PY'
import json, os, sys
out = sys.argv[1]
base = json.load(open(os.path.join(out, "results-baseline.json")))
avx = None
p = os.path.join(out, "results-avx2.json")
if os.path.exists(p): avx = json.load(open(p))
e = base["environment"]
print(f"\n  {e['cpu']}")
print(f"  {e['physical_cores']} physical / {e['logical_cores']} logical cores, {e['compiler']}")
print(f"  SIMD compiled: {e['simd_compiled']}, CPU supports: {e['simd_available_on_cpu']}\n")
print(f"  {'thr':>4} {'baseline M paths/s':>19}" + (f" {'AVX2 M paths/s':>16} {'gain':>6}" if avx else ""))
rows = zip(base["monte_carlo"]["scaling"], avx["monte_carlo"]["scaling"]) if avx else ((r, None) for r in base["monte_carlo"]["scaling"])
for rb, ra in rows:
    smt = " (SMT)" if rb["oversubscribed_smt"] else ""
    line = f"  {rb['threads']:>4} {rb['paths_per_sec']/1e6:>19.1f}"
    if ra: line += f" {ra['paths_per_sec']/1e6:>16.1f} {ra['paths_per_sec']/rb['paths_per_sec']:>5.2f}x"
    print(line + smt)
if not base["monte_carlo"]["eight_physical_core_measured"]:
    print(f"\n  8-physical-core: NOT MEASURED on this host. No number is reported or extrapolated.")
PY
