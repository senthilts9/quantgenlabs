# Benchmark methodology

Every performance number the Performance Lab shows is produced by
`cpp/src/mkt_bench.cpp` and written to `benchmarks/results-*.json`. Nothing is
typed into a UI component. To reproduce:

```bash
./benchmarks/run.sh                 # 20M paths, 5 repetitions
./benchmarks/run.sh 50000000 7      # more paths, more repetitions
```

The script builds the harness twice from identical source — once with no SIMD
flags, once with `-mavx2 -mfma` — and writes both result files. The two-build
comparison isolates the contribution of vectorisation *on the host doing the
measuring*, rather than importing a speedup claim from a different machine.

## What is recorded, and why

A throughput figure is not a fact about code. It is a fact about code, on a CPU,
compiled by a particular compiler at a particular optimisation level, running a
particular model. Quoting the number without those is what makes benchmark claims
unfalsifiable, so the harness records all of them in every result file:

| Field | Why it is needed |
|---|---|
| `cpu`, `arch` | Throughput is per-microarchitecture. |
| `physical_cores`, `logical_cores` | Scaling efficiency is meaningless without both — see *SMT* below. |
| `compiler`, `flags`, `optimised` | An `-O0` build produces numbers that mean nothing; the harness labels itself as unoptimised if built that way. |
| `simd_compiled` vs `simd_available_on_cpu` | The gap between what the CPU supports and what the binary targets is itself a finding. |
| `timesteps_per_path` | See below. This is the single most important disclosure. |
| `repetitions`, `spread_pct` | One sample on a desktop is a draw from a noisy distribution, not a measurement. |

## The timestep disclosure

The Monte Carlo engine prices European options by sampling the **terminal value**
of a geometric Brownian motion: **one timestep per path**.

A 252-step path-dependent simulation does roughly 252× the work per path. A
terminal-value `paths/sec` figure is therefore **not comparable** to a
path-dependent one, and comparing them is the easiest way to make a Monte Carlo
engine look orders of magnitude faster than it is. Every result file states
`"timesteps_per_path": 1` alongside the throughput.

## SMT and why 4 threads is not 4 cores

The development host reports 4 logical CPUs from **2 physical cores**. Two threads
on two physical cores is real parallelism. Four threads on those same two cores is
simultaneous multithreading: the threads share execution units, so an ALU-bound
workload cannot reach 4×.

Every scaling row is tagged `oversubscribed_smt`. A reader who sees
"4 threads: 2.8×" without that tag would reasonably infer that 8 threads gives
~5.7×, which is false on this hardware. The tag exists to prevent that inference.

## The 8-core question

The original performance objective was ~34M paths/sec single-thread scaling to
~250M paths/sec on 8 cores.

- The single-thread target **is met and exceeded** on this host: see the measured
  figures below.
- The 8-core figure is **not measured and not reported**. This host has 2 physical
  cores. `eight_physical_core_measured` is `false` in every result file, with a
  note naming the actual core count.

No extrapolation is published. Multiplying a measured per-core figure by 8 would
produce a number that looks like a measurement and is not one. The harness is
written so that running it on an 8-core host populates the field automatically —
the honest way to fill it is to run it there.

## Measured results

Host: Intel Core i5-7360U @ 2.30GHz, **2 physical / 4 logical cores**, macOS,
Apple clang 14.0.3, `-O3 -std=c++20`. CPU supports AVX2+FMA; no AVX-512.
20,000,000 paths, **median of 7 repetitions**, nothing else running.

### Read the spread before the median

This is a 15W mobile CPU under an ordinary desktop OS. It thermally throttles and
it is preempted, so run-to-run variation is large — up to 43% on the 4-thread
Monte Carlo row and 79% on depth snapshots. Medians alone would imply a precision
these measurements do not have, so best/worst/spread are recorded for every figure
and reproduced here.

The consequence: **treat the ratios as the result and the absolute numbers as
approximate.** The ratios are stable across runs; the absolute values are not.
An earlier run of this same harness while the machine was busy produced a
4-thread baseline figure *below* its own 2-thread figure — which is what
contention looks like, and why `run.sh` should be run on an otherwise idle host.

### Monte Carlo — European call, GBM, terminal-value, antithetic variates

Baseline build (no SIMD flags):

| Threads | Median | Best | Worst | Spread | Speedup | Efficiency | |
|---|---|---|---|---|---|---|---|
| 1 | 39.4M paths/s | 41.0M | 35.8M | 13.9% | 1.00× | 1.00 | |
| 2 | 78.8M paths/s | 80.3M | 70.2M | 14.1% | 2.00× | 1.00 | real cores |
| 4 | 102.8M paths/s | 105.6M | 73.2M | 43.0% | 2.61× | 0.65 | **SMT — not 4 cores** |

AVX2+FMA build, identical source:

| Threads | Median | Best | Worst | Spread | Speedup | Efficiency | Gain vs baseline |
|---|---|---|---|---|---|---|---|
| 1 | 70.9M paths/s | 72.1M | 51.5M | 39.4% | 1.00× | 1.00 | **1.80×** |
| 2 | 128.1M paths/s | 130.4M | 106.2M | 22.4% | 1.81× | 0.90 | 1.63× |
| 4 | 153.3M paths/s | 166.0M | 129.7M | 25.8% | 2.16× | 0.54 | 1.49× |

Four results worth stating explicitly:

- **The 34M paths/sec single-thread objective is met with no SIMD flags at all,
  and it holds at worst case** — the slowest of 7 baseline repetitions was 35.8M,
  still above target. With AVX2 the worst case is 51.5M.
- **Vectorisation is worth roughly 1.5–1.9×** on this workload. That ratio
  reproduced across three independent runs (1.67×, 1.93×, 1.80× at one thread)
  even though the absolute figures moved, which is why it is quoted as a range.
- **Vectorisation costs no accuracy here.** The AVX2 build returns a price
  *bitwise-identical* to the scalar build — 9.415557633681 in both. The SIMD path
  is not trading correctness for throughput.
- **Two threads on two physical cores scales essentially perfectly** (2.00×,
  efficiency 1.00 in the baseline build). The fall-off at 4 threads is SMT, not a
  defect in the parallel decomposition.

### Determinism

`bitwise_identical_across_thread_counts: true` in both builds: the parallel engine
returns the identical price at 1, 2 and 4 threads, *and* the AVX2 build agrees
bitwise with the scalar build.

This holds because per-block RNG substreams are seeded from
`(base_seed, block_index)` and partial sums are reduced in block-index order.
Floating-point addition is not associative, so a fixed reduction order is what
makes the result reproducible rather than merely close — and block boundaries come
from a fixed `block_pairs`, never from the thread count, so the decomposition
cannot disturb it.

### Connectivity core (single thread, in-process, baseline build)

| Operation | Median | Spread | |
|---|---|---|---|
| Top-of-book reads (`micro_price`) | 380.4M/s | 13.4% | |
| FIX field lookups by tag | 143.6M/s | 8.8% | |
| Pre-trade risk checks | 89.0M/s | 4.3% | |
| Depth snapshots (10 levels, no allocation) | 72.2M/s | **78.6%** | unreliable — see below |
| Feed validations | 29.3M/s | 7.8% | |
| Order book updates | 24.8M/s | 4.5% | |
| FIX encode (NewOrderSingle, 11 fields) | 6.6M/s | 3.3% | |
| **End-to-end order path** | **5.2M/s** | 6.8% | |
| FIX parse, framing trusted | 2.9M/s | 1.9% | |
| FIX parse, BodyLength+CheckSum validated | 2.6M/s | 4.2% | |

**The depth-snapshot figure should not be quoted.** Its 78.6% spread means the
measurement is dominated by scheduling noise rather than by the code under test.
It is left in the table rather than deleted because silently dropping the one
inconvenient row is how benchmark tables become dishonest; the correct response is
to label it unreliable and, if the number matters, measure it on a quieter host.

The end-to-end figure covers: validate tick → update book → read top of book →
clear pre-trade risk → encode `NewOrderSingle`.

Prefer `fix_parse_validated` in any comparison: it is the figure a real engine
pays. Skipping BodyLength and CheckSum is only legitimate when replaying a log
whose framing is already trusted.

## Latency distribution (the HFT question)

Everything above is **throughput**. Throughput is the right measure for a batch
job and the wrong one for a trading path, because it hides the number that decides
whether a system is usable: the tail.

A path that averages 200ns with a p99.9 of 50µs is not a 200ns path. It misses one
message in a thousand by a margin that loses the trade — and a mean cannot see it,
because one 50µs sample among a thousand 200ns samples moves the mean by 50ns,
which looks like noise. `cpp/src/lat_bench.cpp` measures the distribution:

```bash
./benchmarks/run.sh          # runs it; writes benchmarks/results-latency.json
```

### The measurement floor comes first

Reading the monotonic clock on this host costs **p50 43ns, p99 59ns**. That is the
same order as the operations being timed, so the harness measures it first and
flags any result whose p50 sits within 25% of it as `at_timer_floor: true`.

Three of the twelve measurements are flagged. **They are not measurements of the
code — they are measurements of the timer**, and they are reported that way rather
than quoted as sub-50ns achievements.

### Measured, 300,000 iterations, nanoseconds per operation

| Operation | p50 | p99 | p99.9 | p99/p50 | |
|---|---|---|---|---|---|
| `book.top_of_book` | 46 | 65 | 68 | 1.4× | **at timer floor** |
| `book.depth_10` | 49 | 70 | 75 | 1.4× | **at timer floor** |
| `risk.pretrade_check` | 53 | 55 | 70 | 1.0× | **at timer floor** |
| `book.upsert` | 72 | 114 | 143 | 1.6× | |
| `feed.validate` | 87 | 91 | 110 | 1.1× | |
| `fix.encode_nos` | 184 | 208 | 285 | 1.1× | |
| **`tick_to_order`** | **209** | **240** | **329** | **1.1×** | full path |
| `fix.parse_trusted` | 344 | 363 | 608 | 1.1× | |
| `fix.parse_validated` | 379 | 393 | 682 | 1.0× | |

`tick_to_order` is validate → book update → top-of-book read → pre-trade risk →
encode `NewOrderSingle`. **209ns p50, 329ns p99.9.** No network, no kernel, no
venue — those dominate real wire-to-wire latency and are not being claimed here.

**Read `p99/p50` first.** Every in-process path is between 1.0× and 1.6×, which
says the median is describing the system rather than hiding a tail behind it.

### Cross-thread handoff — why a mutex is the wrong primitive

One message across an **empty** queue, producer paced so nothing queues:

| Mechanism | p50 | p99 | p99.9 |
|---|---|---|---|
| Lock-free SPSC ring | **125 ns** | 158 ns | **172 ns** |
| Same ring, indices sharing one cache line | 144 ns | 158 ns | 165 ns |
| `std::queue` + mutex + condvar | 4,953 ns | 10,096 ns | **113,504 ns** |

The p50 gap is 40×. **The p99.9 gap is 660×** — and that is the real argument. A
mutex is not merely slower on average: its tail is unbounded, because the wake-up
is a syscall and a producer descheduled while holding the lock stops the consumer
for a whole scheduling quantum. That is the p99.9 nobody can explain afterwards.

False sharing costs ~15% at p50 here. That is smaller than it would be on a bigger
machine — this host has 2 physical cores sharing one L3, so the line ping-pong is
cheap. Reported as measured rather than as the dramatic number it becomes across
sockets.

### A measurement bug worth recording

The first version of the handoff benchmark ran the producer flat out and
timestamped at push. It reported **~29µs for the lock-free ring and ~18ms for the
mutex queue**. Both were real numbers and neither meant what the label said: the
producer fills a 4,096-slot ring far faster than the consumer drains it, so each
sample included the wait behind thousands of predecessors. That measures
**queueing delay under saturation**, not handoff cost.

Handoff latency is only defined when the queue is not backed up, so the producer
now waits for each message to be taken before sending the next. The corrected
figures are the ones above.

## What these numbers are not

- **Not wire-to-wire latency.** The end-to-end path excludes network, kernel and
  venue time, which dominate real latency. It measures the connectivity layer's
  own cost.
- **Not production-tuned.** Measured on a loaded desktop OS with no CPU pinning,
  no isolated cores, no hugepages and no kernel-bypass networking. Treat them as
  relative comparisons between implementations on one host.
- **Latency and throughput are separate sections**, measured by separate harnesses,
  and neither figure should be quoted as the other.
- **The tails describe this host, not the code.** No CPU pinning, no isolated
  cores, no tickless kernel, no hugepages, no kernel-bypass NIC. A real
  low-latency deployment controls all four. The honest reading: **p50 tells you
  about the code, the tail tells you about this machine.**

The reason to prefer `fix_parse_validated` over `fix_parse_unvalidated` in any
comparison is that the validated figure is the one a real engine pays: skipping
BodyLength and CheckSum is only legitimate when replaying a log whose framing is
already trusted.
