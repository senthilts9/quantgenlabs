cmake_minimum_required(VERSION 3.20)
project(quantgenlabs_engines LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()
add_compile_options(-O3 -Wall -Wextra)

add_library(engines INTERFACE)
target_include_directories(engines INTERFACE ${CMAKE_CURRENT_SOURCE_DIR}/include)

find_package(Threads REQUIRED)

foreach(bench quant_bench parallel_bench lsm_bench ns_bench)
  add_executable(${bench} src/${bench}.cpp)
  target_link_libraries(${bench} PRIVATE engines Threads::Threads)
endforeach()

foreach(cbench crypto_sim crypto_ns_bench)
  add_executable(${cbench} src/${cbench}.cpp)
  target_link_libraries(${cbench} PRIVATE engines)
endforeach()

find_package(pybind11 CONFIG QUIET)
if(pybind11_FOUND)
  pybind11_add_module(quant_engine_cpp src/quant_bindings.cpp)
  target_link_libraries(quant_engine_cpp PRIVATE engines Threads::Threads)
  pybind11_add_module(qgl_xrpl_crypto src/crypto_bindings.cpp)
  target_link_libraries(qgl_xrpl_crypto PRIVATE engines)
  message(STATUS "pybind11 found -> building quant_engine_cpp + qgl_xrpl_crypto")
else()
  message(STATUS "pybind11 not found -> Python uses numpy core (engines.py falls back)")
endif()

# --- tests (zero-dependency microtest harness) ---
enable_testing()
add_executable(engine_tests tests/test_engines.cpp)
target_link_libraries(engine_tests PRIVATE engines Threads::Threads)
target_include_directories(engine_tests PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/tests)
add_test(NAME engine_tests COMMAND engine_tests)
