#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

FIRTOOL_ROOT="${FIRTOOL_ROOT:-${DELTARTL_FIRTOOL_ROOT:-}}"
if [[ -z "$FIRTOOL_ROOT" ]]; then
  echo "set FIRTOOL_ROOT to a CIRCT/firtool installation" >&2
  exit 2
fi
BUILD_DIR="${BUILD_DIR:-/tmp/difuzzrtl-modern-regcov-build}"
FIRTOOL="${FIRTOOL_ROOT}/bin/firtool"
PLUGIN="${BUILD_DIR}/libDifuzzRTLModernRegCoverage.so"
VERILATOR="${VERILATOR:-verilator}"

cmake -S "${ROOT_DIR}" -B "${BUILD_DIR}" -DFIRTOOL_ROOT="${FIRTOOL_ROOT}" >/dev/null
cmake --build "${BUILD_DIR}" >/dev/null

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT
ZERO_INIT_DIR="${TMP_DIR}/zero-init"

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
    --low-firrtl-pass-plugin="firrtl.circuit(difuzzrtl-modern-regcoverage-covsum{state-plan=${plan} target-module=TargetRoot coverage-init-dir=${ZERO_INIT_DIR}})" \
    --ir-fir >"${TMP_DIR}/target-${plan}.mlir" 2>&1
  grep -q '%TargetRoot_state_read, %TargetRoot_state_write = firrtl.mem' "${TMP_DIR}/target-${plan}.mlir"
  grep -q '%TargetLeaf_state_read, %TargetLeaf_state_write = firrtl.mem' "${TMP_DIR}/target-${plan}.mlir"
  if grep -Eq '%(Top|Outside)_(state|cov|covSum|metaAssert)_read, .* = firrtl.mem' "${TMP_DIR}/target-${plan}.mlir"; then
    echo "target-module selected state outside TargetRoot" >&2
    exit 1
  fi
  if grep -E 'firrtl\.module private @Outside\(.*(io_covSum|metaAssert|metaReset)' "${TMP_DIR}/target-${plan}.mlir"; then
    echo "target-module added ports to an unrelated module" >&2
    exit 1
  fi
  grep -q 'firrtl.strictconnect %io_covSum, %target_io_covSum' "${TMP_DIR}/target-${plan}.mlir"
  grep -q 'firrtl.strictconnect %metaAssert, %target_metaAssert' "${TMP_DIR}/target-${plan}.mlir"
  grep -q 'firrtl.strictconnect %target_metaReset, %metaReset' "${TMP_DIR}/target-${plan}.mlir"
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
  --low-firrtl-pass-plugin="firrtl.circuit(difuzzrtl-modern-regcoverage-covsum{target-module=TargetRoot coverage-init-dir=${ZERO_INIT_DIR}})" \
  --disable-output >"${TMP_DIR}/target-shared.log" 2>&1; then
  echo "expected cross-boundary shared target descendant to fail" >&2
  exit 1
fi
grep -q 'is not instance-exact' "${TMP_DIR}/target-shared.log"

if "${FIRTOOL}" "${ROOT_DIR}/tests/target-multiple.fir" \
  --mlir-print-op-on-diagnostic=false \
  --load-pass-plugin="${PLUGIN}" \
  --low-firrtl-pass-plugin="firrtl.circuit(difuzzrtl-modern-regcoverage-covsum{target-module=TargetRoot coverage-init-dir=${ZERO_INIT_DIR}})" \
  --disable-output >"${TMP_DIR}/target-multiple.log" 2>&1; then
  echo "expected a non-unique outer observation path to fail" >&2
  exit 1
fi
grep -q 'requires exactly one observation-path child' "${TMP_DIR}/target-multiple.log"

"${FIRTOOL}" "${ROOT_DIR}/tests/target-detached.mlir" \
  --mlir-print-op-on-diagnostic=false \
  --load-pass-plugin="${PLUGIN}" \
  --low-firrtl-pass-plugin="firrtl.circuit(difuzzrtl-modern-regcoverage-covsum{coverage-init-dir=${ZERO_INIT_DIR}})" \
  --disable-output >"${TMP_DIR}/target-detached-default.log" 2>&1

if "${FIRTOOL}" "${ROOT_DIR}/tests/target-detached.mlir" \
  --mlir-print-op-on-diagnostic=false \
  --load-pass-plugin="${PLUGIN}" \
  --low-firrtl-pass-plugin="firrtl.circuit(difuzzrtl-modern-regcoverage-covsum{target-module=TargetRoot coverage-init-dir=${ZERO_INIT_DIR}})" \
  --disable-output >"${TMP_DIR}/target-detached.log" 2>&1; then
  echo "expected a disconnected public observation path to fail" >&2
  exit 1
fi
grep -q 'has a disconnected observation path' "${TMP_DIR}/target-detached.log"

if "${FIRTOOL}" "${ROOT_DIR}/tests/target.fir" \
  --mlir-print-op-on-diagnostic=false \
  --load-pass-plugin="${PLUGIN}" \
  --low-firrtl-pass-plugin="firrtl.circuit(difuzzrtl-modern-regcoverage-covsum{target-module=Missing coverage-init-dir=${ZERO_INIT_DIR}})" \
  --disable-output >"${TMP_DIR}/target-missing.log" 2>&1; then
  echo "expected missing target module to fail" >&2
  exit 1
fi
grep -q 'target module `Missing` does not exist' "${TMP_DIR}/target-missing.log"

if "${FIRTOOL}" "${ROOT_DIR}/tests/simple.fir" \
  --mlir-print-op-on-diagnostic=false \
  --load-pass-plugin="${PLUGIN}" \
  --low-firrtl-pass-plugin='firrtl.circuit(difuzzrtl-modern-regcoverage-covsum)' \
  --disable-output >"${TMP_DIR}/missing-init.log" 2>&1; then
  echo "expected missing coverage-init-dir to fail" >&2
  exit 1
fi
grep -q 'requires an absolute coverage-init-dir' "${TMP_DIR}/missing-init.log"

BAD_INIT_DIR="${TMP_DIR}/bad-init"
mkdir -p "${BAD_INIT_DIR}"
printf '0\n' >"${BAD_INIT_DIR}/zeros-1.hex"
printf '0\n' >"${BAD_INIT_DIR}/zeros-2.hex"
if "${FIRTOOL}" "${ROOT_DIR}/tests/simple.fir" \
  --mlir-print-op-on-diagnostic=false \
  --load-pass-plugin="${PLUGIN}" \
  --low-firrtl-pass-plugin="firrtl.circuit(difuzzrtl-modern-regcoverage-covsum{coverage-init-dir=${BAD_INIT_DIR}})" \
  --disable-output >"${TMP_DIR}/short-init.log" 2>&1; then
  echo "expected short coverage initialization file to fail" >&2
  exit 1
fi
grep -q 'has 1 entries; expected 2' "${TMP_DIR}/short-init.log"

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
    --low-firrtl-pass-plugin="firrtl.circuit(difuzzrtl-modern-regcoverage-covsum{state-plan=${plan} coverage-init-dir=${ZERO_INIT_DIR}})" \
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
        "%Top_state_read, %Top_state_write = firrtl.mem",
        "%Top_cov_read, %Top_cov_write = firrtl.mem",
        "%Top_covSum_read, %Top_covSum_write = firrtl.mem",
    ):
        if token not in text:
            raise SystemExit(f"{path.name}: missing {token}")
    if "io_state" in text:
        raise SystemExit(f"{path.name}: io_state must not be emitted")
    if not re.search(r"firrtl\.strictconnect %r, %a", text):
        raise SystemExit(f"{path.name}: instrumentation changed original register r")
    if "%Top_metaAssert_read, %Top_metaAssert_write = firrtl.mem" in text:
        raise SystemExit(f"{path.name}: unexpected metaAssert register")
    if not re.search(r"firrtl\.strictconnect %metaAssert, %c0_ui1", text):
        raise SystemExit(f"{path.name}: empty metaAssert must be zero")
    if not re.search(r"firrtl\.strictconnect %io_covSum, %\w+", text):
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
    --low-firrtl-pass-plugin="firrtl.circuit(difuzzrtl-modern-regcoverage-covsum{state-plan=${plan} coverage-init-dir=${ZERO_INIT_DIR}})" \
    --verilog -o "${TMP_DIR}/simple-${plan}.sv" >/dev/null 2>&1
  python3 - "${TMP_DIR}/simple-${plan}.sv" "${ZERO_INIT_DIR}" <<'PY'
from __future__ import annotations

import re
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text(encoding="utf-8")
init_dir = str(Path(sys.argv[2]).resolve())
blocks = [
    block for block in re.findall(r"initial begin.*?end // initial", text, re.S)
    if "$readmemh" in block and init_dir in block
]
if len(blocks) < 3:
    raise SystemExit("inserted state/bitmap/covSum memories are not initialized")
for block in blocks:
    random = block.find("`ifdef RANDOMIZE_MEM_INIT")
    if random < 0 or block.find("$readmemh") > random:
        raise SystemExit("expected CIRCT 1.59 $readmemh before optional SV randomization")
for filename in ("zeros-1.hex", "zeros-2.hex"):
    if filename not in text:
        raise SystemExit(f"missing expected initialization file {filename}")
PY
  "${VERILATOR}" --binary --timing -Wno-fatal -DINSTRUMENTED --top-module tb \
    --Mdir "${TMP_DIR}/obj-${plan}" -o sim \
    "${TMP_DIR}/simple-${plan}.sv" "${ROOT_DIR}/tests/noninterference_tb.sv" \
    >/dev/null 2>&1
  "${TMP_DIR}/obj-${plan}/sim" | grep '^TRACE ' >"${TMP_DIR}/${plan}.trace"
  cmp "${TMP_DIR}/baseline.trace" "${TMP_DIR}/${plan}.trace"

  "${VERILATOR}" --binary --timing -Wno-fatal --top-module tb \
    --Mdir "${TMP_DIR}/obj-init-${plan}" -o sim \
    "${TMP_DIR}/simple-${plan}.sv" "${ROOT_DIR}/tests/coverage_init_tb.sv" \
    >/dev/null 2>&1
  for seed in 1 987654; do
    "${TMP_DIR}/obj-init-${plan}/sim" \
      +verilator+rand+reset+2 +verilator+seed+${seed} \
      | grep '^COV ' >"${TMP_DIR}/${plan}-${seed}.cov"
  done
  cmp "${TMP_DIR}/${plan}-1.cov" "${TMP_DIR}/${plan}-987654.cov"
done

"${FIRTOOL}" "${ROOT_DIR}/tests/stop.fir" \
  --mlir-print-op-on-diagnostic=false \
  --load-pass-plugin="${PLUGIN}" \
  --low-firrtl-pass-plugin="firrtl.circuit(difuzzrtl-modern-regcoverage-covsum{coverage-init-dir=${ZERO_INIT_DIR}})" \
  --ir-fir >"${TMP_DIR}/stop-covsum.mlir" 2>&1
grep -q '%Top_metaAssert_read, %Top_metaAssert_write = firrtl.mem' "${TMP_DIR}/stop-covsum.mlir"
python3 - "${TMP_DIR}/stop-covsum.mlir" <<'PY'
from __future__ import annotations

import re
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text(encoding="utf-8")
if not re.search(r"firrtl\.mux\(%metaReset, [^,]+, %\w+\)", text):
    raise SystemExit("stop-backed metaAssert metadata is not guarded by metaReset")
if not re.search(r"firrtl\.strictconnect %metaAssert, %\w+", text):
    raise SystemExit("stop-backed metaAssert output is not connected")
PY

"${FIRTOOL}" "${ROOT_DIR}/tests/multiclock.fir" \
  --mlir-print-op-on-diagnostic=false \
  --load-pass-plugin="${PLUGIN}" \
  --low-firrtl-pass-plugin="firrtl.circuit(difuzzrtl-modern-regcoverage-covsum{coverage-init-dir=${ZERO_INIT_DIR}})" \
  --ir-fir >"${TMP_DIR}/multiclock-covsum.mlir" 2>&1
grep -q '%Top_state_read, %Top_state_write = firrtl.mem' "${TMP_DIR}/multiclock-covsum.mlir"
grep -q '%Top_covSum_read, %Top_covSum_write = firrtl.mem' "${TMP_DIR}/multiclock-covsum.mlir"

"${FIRTOOL}" "${ROOT_DIR}/tests/instance.fir" \
  --mlir-print-op-on-diagnostic=false \
  --load-pass-plugin="${PLUGIN}" \
  --low-firrtl-pass-plugin="firrtl.circuit(difuzzrtl-modern-regcoverage-covsum{coverage-init-dir=${ZERO_INIT_DIR}})" \
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
  --low-firrtl-pass-plugin="firrtl.circuit(difuzzrtl-modern-regcoverage-covsum{coverage-init-dir=${ZERO_INIT_DIR}})" \
  --verilog -o "${TMP_DIR}/instance-covsum.sv" >/dev/null 2>&1

echo "DifuzzRTL modern regCoverage smoke passed."
