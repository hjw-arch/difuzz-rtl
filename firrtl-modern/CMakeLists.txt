cmake_minimum_required(VERSION 3.20)

project(DifuzzRTLModernRegCoverage LANGUAGES CXX C)

set(FIRTOOL_ROOT
  "/home/hjw-arch/FuzzerBenchmark/.cache/rocket-tools/firtool-1.59.0"
  CACHE PATH "CIRCT/firtool installation used to build and load the plugin")

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED YES)
set(CMAKE_CXX_EXTENSIONS NO)

add_library(DifuzzRTLModernRegCoverage MODULE
  src/ModernRegCoverage.cpp
)

execute_process(
  COMMAND "${FIRTOOL_ROOT}/bin/llvm-config" --cxxflags
  OUTPUT_VARIABLE LLVM_CONFIG_CXXFLAGS
  OUTPUT_STRIP_TRAILING_WHITESPACE
  COMMAND_ERROR_IS_FATAL ANY
)
separate_arguments(LLVM_CONFIG_CXXFLAGS NATIVE_COMMAND "${LLVM_CONFIG_CXXFLAGS}")

target_compile_options(DifuzzRTLModernRegCoverage PRIVATE ${LLVM_CONFIG_CXXFLAGS})

target_include_directories(DifuzzRTLModernRegCoverage
  PRIVATE
    "${FIRTOOL_ROOT}/include"
)

target_link_directories(DifuzzRTLModernRegCoverage PRIVATE "${FIRTOOL_ROOT}/lib")

target_link_libraries(DifuzzRTLModernRegCoverage
  PRIVATE
    CIRCTFIRRTL
    MLIRIR
    MLIRPass
    MLIRSupport
    LLVMSupport
)

target_link_options(DifuzzRTLModernRegCoverage
  PRIVATE
    "-Wl,-rpath,${FIRTOOL_ROOT}/lib"
)

set_target_properties(DifuzzRTLModernRegCoverage PROPERTIES
  PREFIX "lib"
  LIBRARY_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}"
)
