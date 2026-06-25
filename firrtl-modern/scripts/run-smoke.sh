#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

FIRTOOL_ROOT="${FIRTOOL_ROOT:-/home/hjw-arch/FuzzerBenchmark/.cache/rocket-tools/firtool-1.59.0}"
BUILD_DIR="${BUILD_DIR:-/tmp/difuzzrtl-modern-regcov-build}"
FIRTOOL="${FIRTOOL_ROOT}/bin/firtool"
PLUGIN="${BUILD_DIR}/libDifuzzRTLModernRegCoverage.so"

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

"${FIRTOOL}" "${ROOT_DIR}/tests/simple.fir" \
  --mlir-print-op-on-diagnostic=false \
  --load-pass-plugin="${PLUGIN}" \
  --low-firrtl-pass-plugin='firrtl.circuit(difuzzrtl-modern-regcoverage-covsum)' \
  --ir-fir >"${TMP_DIR}/simple-covsum.mlir" 2>&1
grep -q '%Top_state = firrtl.reg' "${TMP_DIR}/simple-covsum.mlir"
grep -q '%Top_cov_read, %Top_cov_write = firrtl.mem' "${TMP_DIR}/simple-covsum.mlir"
grep -q '%Top_covSum = firrtl.reg' "${TMP_DIR}/simple-covsum.mlir"
if grep -q 'io_state' "${TMP_DIR}/simple-covsum.mlir"; then
  echo "io_state must not be emitted without export-state" >&2
  exit 1
fi
python3 - "${TMP_DIR}/simple-covsum.mlir" <<'PY'
from __future__ import annotations

import re
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text(encoding="utf-8")
if not re.search(
    r"(%\w+) = firrtl\.mux\(%metaReset, [^,]+, %a\).*?\n\s*firrtl\.strictconnect %r, \1",
    text,
):
    raise SystemExit("original register r is not guarded by metaReset")
for reg in ("Top_state", "Top_covSum"):
    match = re.search(rf"firrtl\.strictconnect %{reg}, (%\w+)", text)
    if not match:
        raise SystemExit(f"{reg} strictconnect not found")
    rhs = re.escape(match.group(1))
    if re.search(rf"{rhs} = firrtl\.mux\(%metaReset,", text):
        raise SystemExit(f"{reg} must persist across metaReset")
if "%Top_metaAssert = firrtl.reg" in text:
    raise SystemExit("metaAssert register must not be inserted when no stop or child assert exists")
if not re.search(r"firrtl\.strictconnect %metaAssert, %c0_ui1", text):
    raise SystemExit("empty metaAssert must be tied to constant zero")
PY
grep -q 'firrtl.strictconnect %io_covSum, %Top_covSum' "${TMP_DIR}/simple-covsum.mlir"

STATE_MAP="${TMP_DIR}/simple-state-map.json"
"${FIRTOOL}" "${ROOT_DIR}/tests/simple.fir" \
  --mlir-print-op-on-diagnostic=false \
  --load-pass-plugin="${PLUGIN}" \
  --low-firrtl-pass-plugin="firrtl.circuit(difuzzrtl-modern-regcoverage-covsum{export-state=true state-map-file=${STATE_MAP}})" \
  --ir-fir >"${TMP_DIR}/simple-covsum-state.mlir" 2>&1
grep -q 'out %io_state: !firrtl.uint<20>' "${TMP_DIR}/simple-covsum-state.mlir"
python3 - "${STATE_MAP}" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
top = data["roots"]["Top"]
assert data["kind"] == "difuzzrtl_regcoverage_state_map_v0"
assert data["slot_bits"] == 20
assert top == [{"slot": 0, "module": "Top", "path": "Top"}]
PY

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
grep -q 'in %c_halt: !firrtl.uint<1>' "${TMP_DIR}/instance-covsum.mlir"
grep -q 'firrtl.strictconnect %c_metaReset' "${TMP_DIR}/instance-covsum.mlir"

"${FIRTOOL}" "${ROOT_DIR}/tests/instance.fir" \
  --mlir-print-op-on-diagnostic=false \
  --load-pass-plugin="${PLUGIN}" \
  --low-firrtl-pass-plugin='firrtl.circuit(difuzzrtl-modern-regcoverage-covsum)' \
  --verilog -o "${TMP_DIR}/instance-covsum.sv" >/dev/null 2>&1

echo "DifuzzRTL modern regCoverage smoke passed."
