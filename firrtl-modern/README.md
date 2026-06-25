# Modern DifuzzRTL RegCoverage

This directory is for the CIRCT/FIRRTL 3.x implementation of DifuzzRTL
`coverage.regCoverage`.

The goal is semantic parity with the legacy Scala transform in
`../firrtl/src/main/scala/coverage`, not a new coverage algorithm.  The modern
implementation must preserve these legacy properties:

1. Run in the FIRRTL pipeline before lowering to HW/Verilog, not on emitted
   Verilog.
2. Treat both modern `firrtl.reg` and `firrtl.regreset` as legacy
   `DefRegister` candidates.
3. Build the same mux-control dependency graph:
   port, wire, node, register, memory, and instance declarations are graph
   nodes; edges come from node values, connect sources, and register reset
   conditions.
4. Select control registers only when they are sources of mux conditions after
   walking through wires and nodes.
5. Preserve the direct-input-register exclusion used by the legacy transform.
6. Preserve the vector-register grouping rule based on shared source location
   before changing any state packing.
7. Preserve `maxStateSize = 20` and `covSumSize = 30`.
8. Preserve the module-local `io_covSum`, `metaAssert`, and `metaReset`
   semantics and instance aggregation.

The current implementation is a modern CIRCT port of the legacy control
register coverage transform.  It is still expected to be checked against real
Rocket/BOOM FIRRTL before replacing the legacy path in experiments, but the
pass is no longer just an audit shell.

Implemented pass entry points:

* `difuzzrtl-modern-regcoverage-audit` checks the legacy target-selection
  semantics on modern Low FIRRTL and emits module/circuit summaries.
* `difuzzrtl-modern-regcoverage-covsum` keeps its historical bring-up name, but
  now inserts the legacy module-local `state`, 1-bit coverage memory, 30-bit
  `covSum`, `io_covSum` output port, `metaAssert` output port, `metaReset`
  input port, per-direct-child `*_halt` inputs, child-instance `io_covSum`
  aggregation, and child-instance `metaReset = metaReset | child_halt` wiring
  on all non-external modules.  By default it does not export hierarchical
  `io_state` ports, so the original cocotb/VPI DifuzzRTL path keeps the same
  structural surface.
* The local state packing is selectable with `state-plan`.
  `compressed` is the default: small useful control registers are packed
  directly, vector registers share offsets across elements, and uncovered
  mux-condition signals are represented as one-bit state contributors.
  `legacy-like` keeps all small control registers in the state hash and avoids
  the vector/mux-condition compression.  State width is capped at 20 bits in
  both modes and `covSum` arithmetic is truncated back to 30 bits.
* `metaReset` wraps the module's original registers and sticky module-local
  `metaAssert` register.  It deliberately does not clear the inserted coverage
  state, coverage bitmap, or `covSum`, because the fuzzer observes cumulative
  coverage across per-test meta resets.  `metaAssert` is the OR of local stop
  conditions and direct child `metaAssert` outputs.

Version-boundary rule:

* The pass must run at the Low FIRRTL boundary through
  `--low-firrtl-pass-plugin`.
* It rejects high-level FIRRTL control structure operations such as
  `firrtl.when`, because legacy `coverage.regCoverage` runs after
  `ExpandWhens`.
* It expects aggregate types to have been lowered to ground-typed ports,
  wires, and registers, matching legacy `LowerTypes`.
* Modern `firrtl.regreset` is treated as the legacy `DefRegister` object with a
  reset expression, while modern `firrtl.reg` maps to the legacy register
  object without reset.
* The legacy coverage bitmap memory uses `DefMemory(..., writeLatency = 1,
  readLatency = 0, ...)`; the equivalent modern FIRRTL memory is therefore
  `readLatency = 0` and `writeLatency = 1`.
* Legacy offset assignment used Scala's process-global random generator when
  the state contributors exceeded 20 bits.  The modern pass keeps the same
  capped 20-bit state space but uses stable name hashing for reproducible
  hardware generation.

Build:

```sh
cmake -S fuzzer/difuzz-rtl/firrtl-modern -B /tmp/difuzzrtl-modern-regcov-build \
  -DFIRTOOL_ROOT=/home/hjw-arch/FuzzerBenchmark/.cache/rocket-tools/firtool-1.59.0
cmake --build /tmp/difuzzrtl-modern-regcov-build
```

Use with firtool:

```sh
firtool input.fir \
  --load-pass-plugin=/tmp/difuzzrtl-modern-regcov-build/libDifuzzRTLModernRegCoverage.so \
  --low-firrtl-pass-plugin='firrtl.circuit(difuzzrtl-modern-regcoverage-audit)' \
  --disable-output
```

The default state plan is `compressed`.  To use the higher-entropy
DifuzzRTL-alignment plan:

```sh
firtool input.fir \
  --load-pass-plugin=/tmp/difuzzrtl-modern-regcov-build/libDifuzzRTLModernRegCoverage.so \
  --low-firrtl-pass-plugin='firrtl.circuit(difuzzrtl-modern-regcoverage-covsum{state-plan=legacy-like})' \
  --verilog -o regcov.v
```

DiffTest coverage feedback uses the same coverage state values, but transports
them through top-level RTL ports instead of Verilator hierarchy access.  Enable
that path explicitly:

```sh
firtool input.fir \
  --load-pass-plugin=/tmp/difuzzrtl-modern-regcov-build/libDifuzzRTLModernRegCoverage.so \
  --low-firrtl-pass-plugin='firrtl.circuit(difuzzrtl-modern-regcoverage-covsum{state-plan=legacy-like export-state=true state-map-file=/tmp/regcoverage_state_map.json})' \
  --verilog -o regcov_difftest.v
```

`state-map-file` records the stable mapping from each `io_state` slot to the
module instance path.  ISAFuzz uses that map for `--target-module`, so instance
subtree targeting stays aligned with the legacy DifuzzRTL idea of collecting
coverage handles from the target subtree.  If coverage feedback is not needed,
omit `export-state` and `state-map-file`; `io_state` is not generated or
aggregated.

Smoke test:

```sh
fuzzer/difuzz-rtl/firrtl-modern/scripts/run-smoke.sh
```
