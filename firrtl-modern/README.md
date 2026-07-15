# Modern DifuzzRTL RegCoverage

This directory is for the CIRCT/FIRRTL 3.x implementation of DifuzzRTL
`coverage.regCoverage`.

The goal is semantic parity with the legacy Scala transform in
`../firrtl/src/main/scala/coverage` for control-register selection and state
mapping, not a new coverage algorithm.  Unlike the legacy transform, modern
instrumentation is observational: it must never change pre-existing DUT
register next-state or reset behavior.  The implementation preserves these
properties:

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
   aggregation, while restricting `metaReset` to inserted metadata state.

The current implementation is a modern CIRCT port of the legacy control
register coverage selection and mapping.  The pass is no longer just an audit
shell; the smoke suite includes sequential non-interference checks for both
state mappings.

Implemented pass entry points:

* `difuzzrtl-modern-regcoverage-audit` checks the legacy target-selection
  semantics on modern Low FIRRTL and emits module/circuit summaries.
* `difuzzrtl-modern-regcoverage-covsum` keeps its historical bring-up name, but
  now inserts the legacy module-local `state`, 1-bit coverage memory, 30-bit
  `covSum`, `io_covSum` output port, `metaAssert` output port, `metaReset`
  input port, child-instance `io_covSum` aggregation, and direct `metaReset`
  propagation on all non-external modules.  It does not export hierarchical `io_state`
  ports; coverage feedback is collected by the cocotb/VPI DifuzzRTL path.
* The local state packing is selectable with `state-plan`.
  `compressed` is the default: small useful control registers are packed
  directly, vector registers share offsets across elements, and uncovered
  mux-condition signals are represented as one-bit state contributors.
  `legacy-like` keeps all small control registers in the state hash and avoids
  the vector/mux-condition compression.  State width is capped at 20 bits in
  both modes and `covSum` arithmetic is truncated back to 30 bits.
  `target-module` changes only which module definitions contribute local
  coverage; it does not change either mapping algorithm.
* `target-module=<exact-name>` limits local coverage and local stop collection
  to every instance of that module definition and its transitive child
  definitions.  Other modules only aggregate `io_covSum`/`metaAssert` and
  propagate `metaReset`.
  A missing target fails, as does a selected descendant definition also
  instantiated outside the selected closure; this keeps module selection
  instance-exact instead of silently instrumenting unrelated hierarchy.
* `metaReset` clears only the inserted sticky module-local `metaAssert`
  register.  It never rewrites a pre-existing DUT register.  It deliberately
  does not clear inserted coverage state, the coverage bitmap, or `covSum`,
  because the fuzzer observes cumulative coverage.  `metaAssert` is the OR of
  selected local stop conditions and direct child `metaAssert` outputs.

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
  --low-firrtl-pass-plugin='firrtl.circuit(difuzzrtl-modern-regcoverage-covsum{target-module=RocketTile state-plan=legacy-like})' \
  --verilog -o regcov.v
```

DiffTest is not a coverage transport in this flow.  Run coverage experiments
through cocotb/Verilator and replay selected programs on DiffTest for
correctness checking.

Smoke test:

```sh
fuzzer/difuzz-rtl/firrtl-modern/scripts/run-smoke.sh
```
