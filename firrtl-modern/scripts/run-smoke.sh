#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

FIRTOOL_ROOT="${FIRTOOL_ROOT:-/home/hjw-arch/FuzzerBenchmark/.cache/rocket-tools/firtool-1.59.0}"
BUILD_DIR="${BUILD_DIR:-/tmp/difuzzrtl-modern-regcov-build}"
FIRTOOL="${FIRTOOL_ROOT}/bin/firtool"
PLUGIN="${BUILD_DIR}/libDifuzzRTLModernRegCoverage.so"
VERILATOR="${VERILATOR:-verilator}"

cmake -S "${ROOT_DIR}" -B "${BUILD_DIR}" -DFIRTOOL_ROOT="${FIRTOOL_ROOT}" >/dev/null
cmake --build "${BUILD_DIR}" >/dev/null

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT

run_low() {
  local name="$1"
  local pass="${2:-difuzzrtl-modern-regcoverage-audit}"
  "${FIRTOOL}" "${ROOT_DIR}/tests/${name}.fir" \
    --mlir-print-op-on-diagnostic=false \
    --load-pass-plugin="${PLUGIN}" \
    --low-firrtl-pass-plugin="firrtl.circuit(${pass})" \
    --disable-output >"${TMP_DIR}/${name}.log" 2>&1
}

run_low simple
grep -q 'ctrl_regs=1' "${TMP_DIR}/simple.log"
grep -q 'reg_state_size=1' "${TMP_DIR}/simple.log"

run_low vector
grep -q 'vector_groups=1' "${TMP_DIR}/vector.log"
grep -q 'state_plan=compressed' "${TMP_DIR}/vector.log"
grep -q 'total_state_bits=2' "${TMP_DIR}/vector.log"

run_low vector 'difuzzrtl-modern-regcoverage-audit{state-plan=legacy-like}'
mv "${TMP_DIR}/vector.log" "${TMP_DIR}/vector-legacy-like.log"
grep -q 'state_plan=legacy-like' "${TMP_DIR}/vector-legacy-like.log"
grep -q 'total_state_bits=4' "${TMP_DIR}/vector-legacy-like.log"

for plan in compressed legacy-like; do
  "${FIRTOOL}" "${ROOT_DIR}/tests/target.fir" \
    --mlir-print-op-on-diagnostic=false \
    --load-pass-plugin="${PLUGIN}" \
    --low-firrtl-pass-plugin="firrtl.circuit(difuzzrtl-modern-regcoverage-covsum{state-plan=${plan} target-module=TargetRoot})" \
    --ir-fir >"${TMP_DIR}/target-${plan}.mlir" 2>&1
  grep -q '%TargetRoot_state = firrtl.reg' "${TMP_DIR}/target-${plan}.mlir"
  grep -q '%TargetLeaf_state = firrtl.reg' "${TMP_DIR}/target-${plan}.mlir"
  if grep -Eq '%(Top|Outside)_state = firrtl.reg' "${TMP_DIR}/target-${plan}.mlir"; then
    echo "target-module selected state outside TargetRoot" >&2
    exit 1
  fi
done

"${FIRTOOL}" "${ROOT_DIR}/tests/target.fir" \
  --mlir-print-op-on-diagnostic=false \
  --load-pass-plugin="${PLUGIN}" \
  --low-firrtl-pass-plugin='firrtl.circuit(difuzzrtl-modern-regcoverage-audit{target-module=TargetRoot})' \
  --disable-output >"${TMP_DIR}/target-audit.log" 2>&1
grep -q 'target_module=TargetRoot modules=2' "${TMP_DIR}/target-audit.log"

if "${FIRTOOL}" "${ROOT_DIR}/tests/target-shared.fir" \
  --mlir-print-op-on-diagnostic=false \
  --load-pass-plugin="${PLUGIN}" \
  --low-firrtl-pass-plugin='firrtl.circuit(difuzzrtl-modern-regcoverage-covsum{target-module=TargetRoot})' \
  --disable-output >"${TMP_DIR}/target-shared.log" 2>&1; then
  echo "expected cross-boundary shared target descendant to fail" >&2
  exit 1
fi
grep -q 'is not instance-exact' "${TMP_DIR}/target-shared.log"

if "${FIRTOOL}" "${ROOT_DIR}/tests/target.fir" \
  --mlir-print-op-on-diagnostic=false \
  --load-pass-plugin="${PLUGIN}" \
  --low-firrtl-pass-plugin='firrtl.circuit(difuzzrtl-modern-regcoverage-covsum{target-module=Missing})' \
  --disable-output >"${TMP_DIR}/target-missing.log" 2>&1; then
  echo "expected missing target module to fail" >&2
  exit 1
fi
grep -q 'target module `Missing` does not exist' "${TMP_DIR}/target-missing.log"

run_low aggregate
grep -q 'regs=1' "${TMP_DIR}/aggregate.log"
grep -q 'ctrl_regs=1' "${TMP_DIR}/aggregate.log"

if "${FIRTOOL}" "${ROOT_DIR}/tests/when.fir" \
  --mlir-print-op-on-diagnostic=false \
  --load-pass-plugin="${PLUGIN}" \
  --high-firrtl-pass-plugin='firrtl.circuit(difuzzrtl-modern-regcoverage-audit)' \
  --disable-output >"${TMP_DIR}/when-high.log" 2>&1; then
  echo "expected high-FIRRTL plugin point to fail for when.fir" >&2
  exit 1
fi
grep -q 'must run at the Low FIRRTL boundary' "${TMP_DIR}/when-high.log"

if "${FIRTOOL}" "${ROOT_DIR}/tests/when.fir" \
  --mlir-print-op-on-diagnostic=false \
  --load-pass-plugin="${PLUGIN}" \
  --high-firrtl-pass-plugin='firrtl.circuit(difuzzrtl-modern-regcoverage-covsum)' \
  --disable-output >"${TMP_DIR}/when-high-covsum.log" 2>&1; then
  echo "expected high-FIRRTL covSum plugin point to fail for when.fir" >&2
  exit 1
fi
grep -q 'must run at the Low FIRRTL boundary' "${TMP_DIR}/when-high-covsum.log"

run_low when
grep -q 'muxes=2' "${TMP_DIR}/when.log"

for plan in compressed legacy-like; do
  "${FIRTOOL}" "${ROOT_DIR}/tests/simple.fir" \
    --mlir-print-op-on-diagnostic=false \
    --load-pass-plugin="${PLUGIN}" \
    --low-firrtl-pass-plugin="firrtl.circuit(difuzzrtl-modern-regcoverage-covsum{state-plan=${plan}})" \
    --ir-fir >"${TMP_DIR}/simple-covsum-${plan}.mlir" 2>&1
done
python3 - "${TMP_DIR}/simple-covsum-compressed.mlir" \
  "${TMP_DIR}/simple-covsum-legacy-like.mlir" <<'PY'
from __future__ import annotations

import re
import sys
from pathlib import Path

for path in map(Path, sys.argv[1:]):
    text = path.read_text(encoding="utf-8")
    for token in (
        "%Top_state = firrtl.reg",
        "%Top_cov_read, %Top_cov_write = firrtl.mem",
        "%Top_covSum = firrtl.reg",
    ):
        if token not in text:
            raise SystemExit(f"{path.name}: missing {token}")
    if "io_state" in text:
        raise SystemExit(f"{path.name}: io_state must not be emitted")
    if not re.search(r"firrtl\.strictconnect %r, %a", text):
        raise SystemExit(f"{path.name}: instrumentation changed original register r")
    if "%Top_metaAssert = firrtl.reg" in text:
        raise SystemExit(f"{path.name}: unexpected metaAssert register")
    if not re.search(r"firrtl\.strictconnect %metaAssert, %c0_ui1", text):
        raise SystemExit(f"{path.name}: empty metaAssert must be zero")
    if not re.search(r"firrtl\.strictconnect %io_covSum, %Top_covSum", text):
        raise SystemExit(f"{path.name}: missing covSum output")
PY

"${FIRTOOL}" "${ROOT_DIR}/tests/simple.fir" \
  --verilog -o "${TMP_DIR}/simple-baseline.sv" >/dev/null 2>&1
"${VERILATOR}" --binary --timing -Wno-fatal --top-module tb \
  --Mdir "${TMP_DIR}/obj-baseline" -o sim \
  "${TMP_DIR}/simple-baseline.sv" "${ROOT_DIR}/tests/noninterference_tb.sv" \
  >/dev/null 2>&1
"${TMP_DIR}/obj-baseline/sim" | grep '^TRACE ' >"${TMP_DIR}/baseline.trace"
for plan in compressed legacy-like; do
  "${FIRTOOL}" "${ROOT_DIR}/tests/simple.fir" \
    --load-pass-plugin="${PLUGIN}" \
    --low-firrtl-pass-plugin="firrtl.circuit(difuzzrtl-modern-regcoverage-covsum{state-plan=${plan}})" \
    --verilog -o "${TMP_DIR}/simple-${plan}.sv" >/dev/null 2>&1
  "${VERILATOR}" --binary --timing -Wno-fatal -DINSTRUMENTED --top-module tb \
    --Mdir "${TMP_DIR}/obj-${plan}" -o sim \
    "${TMP_DIR}/simple-${plan}.sv" "${ROOT_DIR}/tests/noninterference_tb.sv" \
    >/dev/null 2>&1
  "${TMP_DIR}/obj-${plan}/sim" | grep '^TRACE ' >"${TMP_DIR}/${plan}.trace"
  cmp "${TMP_DIR}/baseline.trace" "${TMP_DIR}/${plan}.trace"
done

"${FIRTOOL}" "${ROOT_DIR}/tests/stop.fir" \
  --mlir-print-op-on-diagnostic=false \
  --load-pass-plugin="${PLUGIN}" \
  --low-firrtl-pass-plugin='firrtl.circuit(difuzzrtl-modern-regcoverage-covsum)' \
  --ir-fir >"${TMP_DIR}/stop-covsum.mlir" 2>&1
grep -q '%Top_metaAssert = firrtl.reg' "${TMP_DIR}/stop-covsum.mlir"
grep -q 'firrtl.strictconnect %metaAssert, %Top_metaAssert' "${TMP_DIR}/stop-covsum.mlir"
python3 - "${TMP_DIR}/stop-covsum.mlir" <<'PY'
from __future__ import annotations

import re
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text(encoding="utf-8")
if not re.search(
    r"(%\w+) = firrtl\.mux\(%metaReset, [^,]+, %\w+\).*?\n\s*firrtl\.strictconnect %Top_metaAssert, \1",
    text,
):
    raise SystemExit("stop-backed metaAssert register is not guarded by metaReset")
PY

"${FIRTOOL}" "${ROOT_DIR}/tests/multiclock.fir" \
  --mlir-print-op-on-diagnostic=false \
  --load-pass-plugin="${PLUGIN}" \
  --low-firrtl-pass-plugin='firrtl.circuit(difuzzrtl-modern-regcoverage-covsum)' \
  --ir-fir >"${TMP_DIR}/multiclock-covsum.mlir" 2>&1
grep -q '%Top_state = firrtl.reg interesting_name %clock1' "${TMP_DIR}/multiclock-covsum.mlir"
grep -q '%Top_covSum = firrtl.reg interesting_name %clock1' "${TMP_DIR}/multiclock-covsum.mlir"

"${FIRTOOL}" "${ROOT_DIR}/tests/instance.fir" \
  --mlir-print-op-on-diagnostic=false \
  --load-pass-plugin="${PLUGIN}" \
  --low-firrtl-pass-plugin='firrtl.circuit(difuzzrtl-modern-regcoverage-covsum)' \
  --ir-fir >"${TMP_DIR}/instance-covsum.mlir" 2>&1
grep -q 'out %io_covSum: !firrtl.uint<30>' "${TMP_DIR}/instance-covsum.mlir"
grep -q 'out io_covSum: !firrtl.uint<30>' "${TMP_DIR}/instance-covsum.mlir"
grep -q 'out %metaAssert: !firrtl.uint<1>' "${TMP_DIR}/instance-covsum.mlir"
grep -q 'in %metaReset: !firrtl.uint<1>' "${TMP_DIR}/instance-covsum.mlir"
grep -q 'firrtl.strictconnect %c_metaReset' "${TMP_DIR}/instance-covsum.mlir"
if grep -q 'c_halt' "${TMP_DIR}/instance-covsum.mlir"; then
  echo "unexpected legacy per-instance halt port" >&2
  exit 1
fi

"${FIRTOOL}" "${ROOT_DIR}/tests/instance.fir" \
  --mlir-print-op-on-diagnostic=false \
  --load-pass-plugin="${PLUGIN}" \
  --low-firrtl-pass-plugin='firrtl.circuit(difuzzrtl-modern-regcoverage-covsum)' \
  --verilog -o "${TMP_DIR}/instance-covsum.sv" >/dev/null 2>&1

echo "DifuzzRTL modern regCoverage smoke passed."
