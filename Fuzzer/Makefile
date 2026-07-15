# Verilator COCOTB Makefile

CWD=$(shell pwd)

SIM=verilator
CMD=verilator

export PYTHONPATH := $(CWD)/../../..:$(CWD):$(CWD)/src:$(CWD)/RTLSim/src:$(PYTHONPATH)
export PATH := $(CWD)/../.local/bin:$(PATH)
PYTHONWARNINGS ?= ignore::DeprecationWarning
export PYTHONWARNINGS

TOPLEVEL_LAGN ?= verilog

TOPLEVEL ?= $(VFILE)
DUT_TOPLEVEL ?= $(TOPLEVEL)
VERILOG_SOURCE ?= $(CWD)/../Benchmarks/Verilog/$(VFILE).v
VERILOG_EXTRA_SOURCES ?=
VERILOG_SOURCES = $(VERILOG_SOURCE) $(VERILOG_EXTRA_SOURCES)
export SIM_BUILD
COCOTB_HDL_TIMEUNIT=1us
COCOTB_HDL_TIMEPRECISION=1us

export DIFUZZRTL_VFILE=$(VERILOG_SOURCES)
export DIFUZZRTL_TOPLEVEL=$(DUT_TOPLEVEL)

OUT ?= output

ifdef DEBUG
    PRINT=$(DEBUG)
    PRINTF_COND=0
else
    PRINT=0
endif

ifdef MULTICORE
    MULTI=-DMULTICORE
    PLUSARGS_OUT=+OUT=$(OUT)
else
    MULTI=
    PLUSARGS_OUT=
endif

ifeq ($(VFILE), RocketTile_VHarness)
    INCLUDE=-I $(CWD)/../Benchmarks/Verilog/RocketTile_mux.v
else
    INCLUDE=
endif

PRINTF_COND ?= 0
STOP_COND ?= 0
VERILATOR_EXTRA_ARGS ?=
VERILATOR_VLT_CONFIG ?=

COMPILE_ARGS=$(MULTI) $(INCLUDE) -DPRINTF_COND=$(PRINTF_COND) -DSTOP_COND=$(STOP_COND) -Wno-PINMISSING $(VERILATOR_EXTRA_ARGS)
PLUSARGS=+DEBUG=$(PRINT) $(PLUSARGS_OUT)

MODULE=DifuzzRTL

CUSTOM_SIM_DEPS=$(CWD)/Makefile
ifneq ($(strip $(VERILATOR_VLT_CONFIG)),)
CUSTOM_COMPILE_DEPS += $(VERILATOR_VLT_CONFIG)
endif

include $(shell cocotb-config --makefiles)/Makefile.inc
ifneq ($(strip $(DELTARTL_COCOTB_SHARE_DIR)),)
COCOTB_SHARE_DIR := $(DELTARTL_COCOTB_SHARE_DIR)
endif
include $(shell cocotb-config --makefiles)/Makefile.sim

ifneq ($(strip $(VERILATOR_VLT_CONFIG)),)
COMPILE_ARGS := $(filter-out --public-flat-rw,$(COMPILE_ARGS)) $(VERILATOR_VLT_CONFIG)
endif

sim: $(MODULE).py
