"""Coverage-only RTL fuzzing entry.

The cocotb runner is deliberately thin:

* ``FUZZER_BACKEND`` chooses the program source (``difuzzrtl`` or ``isafuzz``).
* ``SEED_SCHEDULER`` chooses the corpus scheduling policy.
* target coverage bits and module totals are the only feedback passed back to
  the generic scheduler layer.

No Spike/checker path is used here; this entry is for throughput and coverage
experiments.
"""

from __future__ import annotations

import os
import random
import time

from cocotb.decorators import coroutine
from cocotb.regression import TestFactory

from RTLSim.host import ASSERTION_FAIL, ILL_MEM, SUCCESS, TIME_OUT, rvRTLhost
from src.coverage_utils import (
    ensure_target_module,
    get_cov_log_header,
    get_cov_log_row,
    get_cov_module_names,
)
from src.env_parser import envParser
from src.mutator import GENERATION, rvMutator
from src.preprocessor import rvPreProcessor


def _mkdir(path: str) -> None:
    if path and not os.path.isdir(path):
        os.makedirs(path)


def _write(path: str, text: str, mode: str = "a") -> None:
    with open(path, mode) as fd:
        fd.write(text)


def _status_name(ret) -> str:
    if ret == SUCCESS:
        return "success"
    if ret == ILL_MEM:
        return "illegal_mem"
    if ret == TIME_OUT:
        return "rtl_limit"
    if ret == ASSERTION_FAIL:
        return "assertion_fail"
    if ret == "compile_fail":
        return "compile_fail"
    return str(ret)


def _total_static_insts(sim_input) -> int:
    return sum(
        int(getattr(word, "len_insts", 0) or 0)
        for word in sim_input.prefix + sim_input.words + sim_input.suffix
    )


def _scheduler_needs_target_bits(
        seed_scheduler: str,
        target_module: str | None,
        dt_group_json: str | None = None) -> bool:
    if not target_module and not dt_group_json:
        return False
    if str(os.getenv("FORCE_TARGET_BITS", "0")).strip().lower() in {
            "1", "true", "yes", "on"}:
        return True
    mode = str(seed_scheduler or "").strip().lower().replace("-", "_")
    aliases = {
        "targetnew": "target_new",
        "new": "target_new",
        "targethigh": "target_high",
        "high": "target_high",
        "priority": "target_high",
    }
    mode = aliases.get(mode, mode)
    if dt_group_json and not target_module:
        return mode in {"sgmu", "target_new", "target_high"}
    return mode in {"sgmu", "target_new"}


def _dt_group_summary(rtl_host) -> dict:
    observation = getattr(rtl_host, "last_dt_group_observation", None)
    observer = getattr(rtl_host, "dt_group", None)
    if observation is None:
        return {
            "selected_bits": int(getattr(observer, "selected_bits", 0) or 0),
            "feedback": 0,
            "states": 0,
            "metadata": 0,
            "missing_ports": "",
            "target_score": 0,
        }
    return {
        "selected_bits": int(getattr(observer, "selected_bits", 0) or 0),
        "feedback": len(getattr(observation, "feedback_targets", ()) or ()),
        "states": len(getattr(observation, "monitor_states", ()) or ()),
        "metadata": len(getattr(observation, "metadata_states", ()) or ()),
        "missing_ports": ",".join(sorted(
            getattr(observation, "missing_ports", ()) or ())),
        "target_score": int(getattr(observation, "target_score", 0) or 0),
    }


def _is_generation_warmup(mutator: rvMutator, phase: int, it: int) -> bool:
    warmup_iters = int(getattr(mutator, "corpus_size", 0) or 0) // 10
    return (
        phase == GENERATION
        and getattr(mutator, "phase_policy", "default") == "default"
        and not getattr(mutator, "no_guide", False)
        and int(it) < warmup_iters
    )


def _status_header() -> str:
    return "\t".join([
        "iter", "phase", "status", "coverage", "target_cov",
        "best_target_cov", "best_total_cov", "target_bits_enabled",
        "target_handles", "rtl_cycles", "target_hits", "target_new",
        "bitmap_target_hits", "bitmap_target_new",
        "dtg_handles", "dtg_selected_bits", "dtg_feedback",
        "dtg_target_hits", "dtg_target_new", "dtg_states", "dtg_metadata",
        "dtg_missing_ports", "dtg_target_score",
        "admitted", "corpus", "main_words", "total_static_insts", "get_s",
        "preprocess_s", "rtl_s", "post_s", "total_s",
        "parent_seed_id", "parent_main_words", "parent_static_insts",
        "select_reason", "select_energy", "select_opportunity",
        "select_conversion", "select_cost_norm", "select_value",
        "select_best_alt_value",
    ]) + "\n"


def _append_status(path: str, row: list) -> None:
    values = []
    for value in row:
        if isinstance(value, float):
            values.append("{:.9f}".format(value))
        else:
            values.append(str(value))
    _write(path, "\t".join(values) + "\n")


def _progress(message: str) -> None:
    if str(os.getenv("COVONLY_PROGRESS", "0")).strip().lower() in {
            "1", "true", "yes", "on"}:
        print("[CovOnlyRTL] {}".format(message), flush=True)


def _setup_covonly(dut, toplevel, template, out, proc_num, debug,
                   no_guide, target_module, seed_scheduler,
                   target_rare_horizon, target_max_energy,
                   phase_policy, fuzzer_backend, target_cost_aware,
                   target_high_decay_window, dt_group_json,
                   dt_group_pair_id, dt_group_feedback_io,
                   dt_group_feedback_bits, dt_group_internal_weight,
                   dt_group_object_weights):
    mutator = rvMutator(
        no_guide=bool(no_guide),
        target_module=target_module,
        seed_scheduler=seed_scheduler,
        target_rare_horizon=target_rare_horizon,
        target_max_energy=target_max_energy,
        phase_policy=phase_policy,
        fuzzer_backend=fuzzer_backend,
        target_cost_aware=bool(target_cost_aware),
        target_high_decay_window=int(target_high_decay_window or 0),
    )
    preprocessor = rvPreProcessor(
        "riscv64-unknown-elf-gcc",
        "riscv64-unknown-elf-elf2hex",
        template,
        out,
        proc_num,
    )
    rtl_sigfile = os.path.join(out, ".rtl_sig_{}.txt".format(proc_num))
    rtl_host = rvRTLhost(
        dut, toplevel, rtl_sigfile, debug=bool(debug),
        dt_group_json=dt_group_json,
        dt_group_pair_id=dt_group_pair_id,
        dt_group_feedback_io=dt_group_feedback_io,
        dt_group_feedback_bits=bool(int(dt_group_feedback_bits or 0)),
        dt_group_internal_weight=int(dt_group_internal_weight or 1),
        dt_group_object_weights=dt_group_object_weights or "",
    )
    return mutator, preprocessor, rtl_host


@coroutine
def RunCovOnly(dut, toplevel,
               num_iter=1, template="Template", out="output", record=0,
               proc_num=0, debug=0, no_guide=0, target_module=None,
               seed_scheduler="sgmu", target_bitmap_sample_period=1,
               target_rare_horizon=8, target_max_energy=8,
               phase_policy="default", fuzzer_backend="difuzzrtl",
               target_cost_aware=0,
               target_high_decay_window=0,
               dt_group_json=None, dt_group_pair_id=None,
               dt_group_feedback_io="auto", dt_group_feedback_bits=0,
               dt_group_internal_weight=1,
               dt_group_object_weights="",
               random_seed=0, cov_log=None, status_log=None,
               start_time=0.0, max_seconds=0):
    assert toplevel in ["RocketTile", "BoomTile"], \
        "{} is not toplevel".format(toplevel)

    if int(random_seed or 0):
        random.seed(int(random_seed))
    else:
        random.seed(time.time() * (int(proc_num) + 1))

    mutator, preprocessor, rtl_host = _setup_covonly(
        dut, toplevel, template, out, proc_num, bool(debug),
        bool(no_guide), target_module, seed_scheduler,
        int(target_rare_horizon), int(target_max_energy),
        phase_policy, fuzzer_backend, bool(int(target_cost_aware or 0)),
        int(target_high_decay_window or 0), dt_group_json,
        dt_group_pair_id, dt_group_feedback_io,
        int(dt_group_feedback_bits or 0), int(dt_group_internal_weight or 1),
        dt_group_object_weights or "")

    module_cov_names = ensure_target_module(
        getattr(rtl_host, "module_cov_names", []),
        target_module)
    rtl_host.coverage.set_module_cov_names(module_cov_names)
    rtl_host.module_cov_names = rtl_host.coverage.module_cov_names
    last_module_covs = {name: 0 for name in module_cov_names}
    last_coverage = 0
    best_total_cov = 0
    best_target_cov = 0
    corpus_count = 0
    target_bits_enabled = _scheduler_needs_target_bits(
        seed_scheduler, target_module, dt_group_json)
    if str(dt_group_json or "").strip() and rtl_host.dt_group.selected_bits <= 0:
        raise ValueError("DT Group manifest has no selected feedback bits")
    max_seconds = float(max_seconds or 0)
    wall_start = float(start_time or time.time())

    for it in range(int(num_iter)):
        if max_seconds > 0 and it > 0 and time.time() - wall_start >= max_seconds:
            break
        _progress("iter {} begin".format(it))
        iter_t0 = time.perf_counter()
        phase_at_get = getattr(mutator, "phase", "-")
        get_s = preprocess_s = rtl_s = post_s = 0.0

        t0 = time.perf_counter()
        sim_input, data = mutator.get(False)
        get_s = time.perf_counter() - t0
        total_static = _total_static_insts(sim_input)
        _progress("iter {} generated words={} static_insts={} get_s={:.3f}".format(
            it, sim_input.num_words, total_static, get_s))

        t0 = time.perf_counter()
        isa_input, rtl_input, _symbols = preprocessor.process(
            sim_input, data, False, write_sim_input=bool(record))
        preprocess_s = time.perf_counter() - t0
        _progress("iter {} preprocessed ok={} preprocess_s={:.3f}".format(
            it, bool(isa_input and rtl_input), preprocess_s))

        if not isa_input or not rtl_input:
            mutator.observe_target_yield_result(set(), admitted=False)
            mutator.update_phase(it)
            if status_log:
                selection = mutator.get_last_selection_summary()
                _append_status(status_log, [
                    it, phase_at_get, "compile_fail", last_coverage, 0,
                    best_target_cov, best_total_cov,
                    int(target_bits_enabled),
                    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, "", 0, 0,
                    len(mutator.corpus), sim_input.num_words, total_static,
                    get_s, preprocess_s, rtl_s, post_s,
                    time.perf_counter() - iter_t0,
                    selection["parent_seed_id"],
                    selection["parent_main_words"],
                    selection["parent_static_insts"],
                    selection["reason"],
                    selection["energy"],
                    selection["opportunity"],
                    selection["conversion"],
                    selection["cost_norm"],
                    selection["value"],
                    selection["best_alt_value"],
                ])
            continue

        t0 = time.perf_counter()
        _progress("iter {} rtl begin max_cycles={}".format(
            it, getattr(rtl_input, "max_cycles", "?")))
        result = yield rtl_host.run_test(
            rtl_input,
            False,
            target_bitmap_module=target_module if target_bits_enabled else None,
            target_bitmap_sample_period=target_bitmap_sample_period,
        )
        rtl_s = time.perf_counter() - t0
        _progress("iter {} rtl done result={} rtl_s={:.3f}".format(
            it, result, rtl_s))
        if len(result) == 3:
            ret, coverage, module_covs = result
        else:
            ret, coverage = result
            module_covs = last_module_covs.copy()

        t0 = time.perf_counter()
        bitmap_target_hit_bits = set(
            getattr(rtl_host, "last_bitmap_target_hits", set())) \
            if target_bits_enabled else set()
        dtg_target_hit_bits = set(
            getattr(rtl_host, "last_dt_group_feedback_hits", set())) \
            if target_bits_enabled else set()
        target_hit_bits = bitmap_target_hit_bits | dtg_target_hit_bits
        target_new_bits = mutator.target_new_bits(target_hit_bits)
        bitmap_target_new_bits = mutator.target_new_bits(bitmap_target_hit_bits)
        dtg_target_new_bits = mutator.target_new_bits(dtg_target_hit_bits)
        add_for_target = bool(target_bits_enabled and target_new_bits)
        add_for_warmup = _is_generation_warmup(mutator, phase_at_get, it)
        post_s = time.perf_counter() - t0

        admitted = False
        if add_for_warmup or coverage > last_coverage or add_for_target:
            exec_cycles = int(getattr(rtl_host, "last_cycles", 0) or 0)
            if record:
                sim_input.save(
                    os.path.join(out, "corpus", "id_{}.si".format(corpus_count)),
                    data,
                )
            corpus_count += 1
            admitted = mutator.add_corpus(
                sim_input, coverage, module_covs, target_hit_bits,
                initial_cost=exec_cycles)
            if coverage > last_coverage:
                last_coverage = coverage
        last_module_covs = module_covs.copy()

        mutator.observe_target_yield_result(
            target_new_bits,
            admitted=admitted,
            cost=int(getattr(rtl_host, "last_cycles", 0) or 0))
        mutator.update_phase(it)

        if cov_log and (record or it == int(num_iter) - 1):
            _write(
                cov_log,
                get_cov_log_row(
                    time.time() - float(start_time or 0.0),
                    it + 1,
                    coverage,
                    module_cov_names,
                    module_covs,
                ),
            )

        if status_log:
            target_cov = int(module_covs.get(target_module, 0) or 0) \
                if target_module else 0
            dtg = _dt_group_summary(rtl_host)
            best_target_cov = max(best_target_cov, target_cov)
            best_total_cov = max(best_total_cov, int(coverage or 0))
            selection = mutator.get_last_selection_summary()
            _append_status(status_log, [
                it, phase_at_get, _status_name(ret), coverage, target_cov,
                best_target_cov, best_total_cov, int(target_bits_enabled),
                int(getattr(rtl_host, "last_target_handle_count", 0) or 0),
                int(getattr(rtl_host, "last_cycles", 0) or 0),
                len(target_hit_bits), len(target_new_bits),
                len(bitmap_target_hit_bits), len(bitmap_target_new_bits),
                int(getattr(rtl_host, "last_dt_group_handle_count", 0) or 0),
                dtg["selected_bits"], dtg["feedback"],
                len(dtg_target_hit_bits), len(dtg_target_new_bits),
                dtg["states"], dtg["metadata"],
                dtg["missing_ports"], dtg["target_score"],
                int(admitted),
                len(mutator.corpus), sim_input.num_words, total_static, get_s,
                preprocess_s, rtl_s, post_s,
                time.perf_counter() - iter_t0,
                selection["parent_seed_id"],
                selection["parent_main_words"],
                selection["parent_static_insts"],
                selection["reason"],
                selection["energy"],
                selection["opportunity"],
                selection["conversion"],
                selection["cost_norm"],
                selection["value"],
                selection["best_alt_value"],
            ])


parser = envParser()
parser.add_option("toplevel", None, "Toplevel module of DUT")
parser.add_option("num_iter", 1, "The number of fuzz iterations")
parser.add_option("template", "Template", "Template test file location")
parser.add_option("out", "output", "Directory to save the result")
parser.add_option("record", 0, "Record corpus and coverage log")
parser.add_option("debug", 0, "Debugging")
parser.add_option("no_guide", 0, "Pure random generation")
parser.add_option("target_module", None, "Target module instance")
parser.add_option("seed_scheduler", "sgmu",
                  "Seed scheduler: sgmu, target_high, target_new, uniform")
parser.add_option("target_bitmap_sample_period", 1,
                  "Sample target coverage address every N cycles")
parser.add_option("target_rare_horizon", 8, "SGMU rarity horizon")
parser.add_option("target_max_energy", 8, "SGMU maximum mutation budget")
parser.add_option("target_cost_aware", 0,
                  "Enable SGMU cost-aware value normalization")
parser.add_option("target_high_decay_window", 0,
                  "Target-high no-yield switch window; 0 disables it")
parser.add_option("phase_policy", "default",
                  "Phase policy: default or mutation_only")
parser.add_option("fuzzer_backend", "difuzzrtl",
                  "Fuzzer backend: difuzzrtl or isafuzz")
parser.add_option("dt_group_json", None, "DeltaRTL DT Group json")
parser.add_option("dt_group_pair_id", None, "DT Group pair id")
parser.add_option("dt_group_feedback_io", "auto",
                  "DT Group input/output feedback: auto, never, always")
parser.add_option("dt_group_feedback_bits", 0,
                  "Enable per-bit DT Group feedback targets")
parser.add_option("dt_group_internal_weight", 1,
                  "Integer weight for DT Group internal feedback")
parser.add_option("dt_group_object_weights", "",
                  "DT object weights, e.g. control_register=4,mux_condition=2")
parser.add_option("random_seed", 0, "Random seed, 0 means wall-clock seed")
parser.add_option("max_seconds", 0.0, "Stop after this many wall-clock seconds")

parser.print_help()
parser.parse_option()

# Cocotb may need a physical wrapper to own writable top-level registers.
# Keep all DifuzzRTL semantics tied to the logical processor top.
if logical_toplevel := os.getenv("DIFUZZRTL_TOPLEVEL"):
    _value, _env, _info = parser.arg_map["toplevel"]
    parser.arg_map["toplevel"] = (logical_toplevel, _env, _info)

out = parser.arg_map["out"][0]
toplevel = parser.arg_map["toplevel"][0]
target_module = parser.arg_map["target_module"][0]
record = parser.arg_map["record"][0]

_mkdir(out)
_mkdir(os.path.join(out, "corpus"))

module_cov_names = ensure_target_module(
    get_cov_module_names(top_name=toplevel),
    target_module)
date = time.strftime("%Y%m%d")
cov_log = os.path.join(out, "cov_log_{}.txt".format(date))
status_log = os.path.join(out, "status.tsv")

if not os.path.isfile(cov_log):
    _write(cov_log, get_cov_log_header(module_cov_names), "w")
if not os.path.isfile(status_log):
    _write(status_log, _status_header(), "w")

start_time = time.time()
factory = TestFactory(RunCovOnly)
parser.register_option(factory)
factory.add_option("cov_log", [cov_log])
factory.add_option("status_log", [status_log])
factory.add_option("start_time", [start_time])
factory.generate_tests()
