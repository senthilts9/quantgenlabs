// microtest.hpp — a ~40-line zero-dependency test harness.
//
// Deliberately tiny so the C++ tests build anywhere with just g++ (no Catch2
// or GoogleTest fetch). Swap for Catch2/GTest later if you want fixtures and
// richer reporting; the test bodies won't change much.
#pragma once
#include <cmath>
#include <cstdio>
#include <functional>
#include <string>
#include <vector>

namespace microtest {
struct Case { std::string name; std::function<void()> fn; };
inline std::vector<Case>& registry() { static std::vector<Case> r; return r; }
inline int& failures() { static int f = 0; return f; }
struct Reg { Reg(const std::string& n, std::function<void()> f) { registry().push_back({n, f}); } };

inline void fail(const char* file, int line, const std::string& msg) {
    std::printf("    ✗ %s:%d  %s\n", file, line, msg.c_str());
    ++failures();
}
inline int run_all() {
    int passed = 0;
    for (auto& c : registry()) {
        const int before = failures();
        c.fn();
        if (failures() == before) { std::printf("  ✓ %s\n", c.name.c_str()); ++passed; }
        else std::printf("  ✗ %s\n", c.name.c_str());
    }
    std::printf("\n%d/%zu tests passed, %d assertion failure(s)\n",
                passed, registry().size(), failures());
    return failures() ? 1 : 0;
}
}  // namespace microtest

#define TEST(name) \
    static void name(); \
    static microtest::Reg reg_##name(#name, name); \
    static void name()

#define REQUIRE(cond) \
    do { if (!(cond)) microtest::fail(__FILE__, __LINE__, "REQUIRE(" #cond ")"); } while (0)

// Like REQUIRE, but RETURNS from the test body on failure. Use it for any
// assertion the rest of the test depends on structurally -- a container size
// before indexing, an optional before dereferencing. Plain REQUIRE records the
// failure and carries on, which for a size check means the next line performs an
// out-of-bounds read and the whole binary dies with a segfault instead of a
// readable failure. (That is not hypothetical: it happened while writing the FIX
// venue tests.)
#define REQUIRE_R(cond) \
    do { if (!(cond)) { microtest::fail(__FILE__, __LINE__, "REQUIRE_R(" #cond ")"); return; } } while (0)

#define CHECK_CLOSE(a, b, tol) \
    do { const double _d = std::fabs((a) - (b)); \
         if (_d > (tol)) microtest::fail(__FILE__, __LINE__, \
            std::string(#a " ~= " #b " (|Δ|=") + std::to_string(_d) + ")"); } while (0)
