#!/usr/bin/env bash
# bench-pinned.sh — produce publishable latency figures on Linux.
#
# WHY THIS SCRIPT EXISTS. Absolute nanosecond figures taken on an unpinned
# laptop are not evidence. During one measurement session the SAME unchanged
# binary drifted 30% as the CPU thermally throttled, while the ratios it
# reported held to within 1%. Ratios survive a noisy machine; absolutes do not.
#
# macOS has no isolcpus and no working taskset, and turbo cannot be pinned
# down, so a rigorous harness is not achievable there at all. This runs on the
# Linux box, pins a core, and prints the harness metadata alongside the numbers
# so anyone reading them can see how they were taken.
#
#   chmod +x deploy/bench-pinned.sh && ./deploy/bench-pinned.sh
#
# Everything it prints is intended to be screenshotted together -- the harness
# block is part of the evidence, not preamble to skip.
set -euo pipefail
cd "$(dirname "$0")/.."

CORE="${CORE:-1}"          # which CPU to pin to; override with CORE=n
OUT="${OUT:-/tmp/qgl-bench}"
mkdir -p "$OUT"

echo "======================================================================"
echo " HARNESS"
echo "======================================================================"
echo "  date            : $(date -u '+%Y-%m-%dT%H:%M:%SZ') (UTC)"
echo "  host            : $(hostname)"
if [ -r /sys/devices/virtual/dmi/id/product_name ]; then
  echo "  instance        : $(cat /sys/devices/virtual/dmi/id/product_name 2>/dev/null || echo '?')"
fi
echo "  cpu             : $(grep -m1 'model name' /proc/cpuinfo | cut -d: -f2- | sed 's/^ *//')"
echo "  physical cores  : $(lscpu | awk -F: '/^Core\(s\) per socket/{gsub(/ /,"",$2); c=$2} /^Socket\(s\)/{gsub(/ /,"",$2); s=$2} END{print c*s}')"
echo "  logical cpus    : $(nproc)"
echo "  pinned to cpu   : ${CORE}  (taskset -c ${CORE})"
echo "  governor        : $(cat /sys/devices/system/cpu/cpu${CORE}/cpufreq/scaling_governor 2>/dev/null || echo 'not exposed (virtualised)')"
echo "  turbo           : $(cat /sys/devices/system/cpu/intel_pstate/no_turbo 2>/dev/null | sed 's/^0$/ENABLED/;s/^1$/disabled/' || echo 'not exposed (virtualised)')"
echo "  compiler        : $(c++ --version | head -1)"
echo "  flags           : -O3 -march=native"
echo "  method          : inputs varied per iteration, results sunk through an"
echo "                    asm barrier, BATCH timed and divided (never a single"
echo "                    sub-microsecond call -- two clock reads cost ~20-25ns"
echo "                    each and would be 20%+ of the measurement)"
echo
echo "  NOTE: on a shared/burstable instance the governor and turbo state are"
echo "        not exposed to the guest, and a burstable family (t*, *-flex)"
echo "        will drift as CPU credits deplete. Report the instance type."
echo

echo "======================================================================"
echo " BUILD"
echo "======================================================================"
c++ -std=c++20 -O3 -march=native -Icpp/include cpp/src/ns_bench.cpp \
    -o "$OUT/ns_bench" -pthread
c++ -std=c++20 -O3 -march=native -Icpp/include cpp/src/crypto_ns_bench.cpp \
    -o "$OUT/crypto_ns_bench"
c++ -std=c++20 -O3 -march=native -Icpp/include cpp/src/parallel_bench.cpp \
    -o "$OUT/parallel_bench" -pthread
c++ -std=c++20 -O3 -march=native -Icpp/include -Icpp/tests \
    cpp/tests/test_engines.cpp -o "$OUT/engine_tests" -pthread
echo "  built ns_bench, parallel_bench, engine_tests"
echo "  AVX2 in this build : $(echo | c++ -dM -E -march=native - | grep -c '__AVX2__' | sed 's/^1$/yes/;s/^0$/NO (scalar fallback)/')"
echo

echo "======================================================================"
echo " CORRECTNESS FIRST — no timing is worth anything if these fail"
echo "======================================================================"
taskset -c "$CORE" "$OUT/engine_tests"
echo

echo "======================================================================"
echo " KERNEL LATENCY (pinned, single core)"
echo "======================================================================"
taskset -c "$CORE" "$OUT/ns_bench"
echo

echo "======================================================================"
echo " CRYPTO PER-TICK RISK LATENCY (pinned, single core)"
echo "======================================================================"
taskset -c "$CORE" "$OUT/crypto_ns_bench"
echo

echo "======================================================================"
echo " PARALLEL SCALING + DETERMINISM (all cores)"
echo "======================================================================"
"$OUT/parallel_bench"
echo

echo "======================================================================"
echo " PYTHON BOUNDARY — the 0.6x, before and after"
echo "======================================================================"
if python3 -c "import quant_engine_cpp" 2>/dev/null; then
  taskset -c "$CORE" python3 deploy/bench_boundary.py
else
  echo "  quant_engine_cpp not importable. Build it first:"
  echo
  echo "    python3 -m pip install pybind11 numpy"
  echo "    c++ -std=c++20 -O3 -march=native -shared -fPIC \\"
  echo "        \$(python3 -m pybind11 --includes) -Icpp/include \\"
  echo "        cpp/src/quant_bindings.cpp \\"
  echo "        -o quant_engine_cpp\$(python3-config --extension-suffix) -pthread"
  echo
  echo "  then re-run this script."
fi

echo
echo "======================================================================"
echo " Screenshot this entire output, harness block included."
echo "======================================================================"
