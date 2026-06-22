import os
import time
import random

from cocotb.decorators import coroutine
from RTLSim.host import ILL_MEM, SUCCESS, TIME_OUT, ASSERTION_FAIL

from src.utils import *
from src.multicore_manager import proc_state
from src.coverage_utils import ensure_target_module, get_cov_log_row


def append_cov_log(record, cov_log, start_time, iteration, total_cov,
                   module_names, module_covs):
    if not (record and cov_log):
        return
    save_file(cov_log, 'a',
              get_cov_log_row(time.time() - start_time,
                              iteration,
                              total_cov,
                              module_names,
                              module_covs))


def timing_trace_enabled():
    return str(os.environ.get('SCHED_TIMING', '')).strip().lower() in [
        '1', 'true', 'yes', 'on'
    ]


def timing_trace_path(out, proc_num):
    explicit = os.environ.get('SCHED_TIMING_LOG', '')
    if explicit:
        return explicit
    return os.path.join(out, 'scheduler_timing_{}.tsv'.format(proc_num))


def init_timing_trace(path):
    if not path or os.path.isfile(path):
        return
    save_file(path, 'w', '\t'.join([
        'iter', 'phase', 'parent_seed', 'ret', 'match',
        'coverage', 'target_cov', 'target_hits', 'target_new',
        'admitted', 'active_remaining', 'energy',
        'get_s', 'preprocess_s', 'isa_s', 'rtl_s',
        'post_s', 'admit_s', 'observe_s', 'phase_s', 'log_s',
        'total_s',
    ]) + '\n')


def append_timing_trace(path, row):
    if not path:
        return
    values = []
    for value in row:
        if isinstance(value, float):
            values.append('{:.9f}'.format(value))
        else:
            values.append(str(value))
    save_file(path, 'a', '\t'.join(values) + '\n')


@coroutine
def Run(dut, toplevel,
        num_iter=1, template='Template', in_file=None,
        out='output', record=False, cov_log=None,
        multicore=0, manager=None, proc_num=0, start_time=0, start_iter=0, start_cov=0,
        prob_intr=0, no_guide=False, debug=False,
        target_module=None, seed_scheduler='sgmu',
        target_bitmap_sample_period=1, target_rare_horizon=8,
        target_max_energy=8, phase_policy='default',
        fuzzer_backend='difuzzrtl', skip_checker=0):

    assert toplevel in ['RocketTile', 'BoomTile' ], \
        '{} is not toplevel'.format(toplevel)

    random.seed(time.time() * (proc_num + 1))

    (mutator, preprocessor, isaHost, rtlHost, checker) = \
        setup(dut, toplevel, template, out, proc_num, debug,
              no_guide=no_guide, target_module=target_module,
              seed_scheduler=seed_scheduler,
              target_rare_horizon=target_rare_horizon,
              target_max_energy=target_max_energy,
              phase_policy=phase_policy,
              fuzzer_backend=fuzzer_backend)

    if in_file: num_iter = 1

    stop = [ proc_state.NORMAL ]
    mNum = 0
    cNum = 0
    iNum = 0
    last_coverage = 0
    module_cov_names = ensure_target_module(
        getattr(rtlHost, 'module_cov_names', []),
        target_module)
    last_module_covs = {
        module_name: 0 for module_name in module_cov_names
    }
    skip_checker = bool(skip_checker)
    timing_path = timing_trace_path(out, proc_num) if timing_trace_enabled() else None
    if timing_path:
        init_timing_trace(timing_path)

    debug_print('[DifuzzRTL] Start Fuzzing', debug)

    if multicore:
        yield manager.cov_restore(dut)

    for it in range(num_iter):
        iter_t0 = time.perf_counter()
        get_s = 0.0
        preprocess_s = 0.0
        isa_s = 0.0
        rtl_s = 0.0
        post_s = 0.0
        admit_s = 0.0
        observe_s = 0.0
        phase_s = 0.0
        log_s = 0.0
        phase_at_get = getattr(mutator, 'phase', '-')
        parent_seed_id = ''
        selected_energy = ''
        active_remaining = ''
        debug_print('[DifuzzRTL] Iteration [{}]'.format(it), debug)

        if multicore:
            if it == 0:
                mutator.update_corpus(out + '/corpus', 1000)
            elif it % 1000 == 0:
                mutator.update_corpus(out + '/corpus')

        assert_intr = False
        if random.random() < prob_intr:
            assert_intr = True

        t0 = time.perf_counter()
        if in_file: (sim_input, data, assert_intr) = mutator.read_siminput(in_file)
        else: (sim_input, data) = mutator.get(assert_intr)
        get_s = time.perf_counter() - t0
        scheduled = getattr(mutator, 'scheduled_corpus', None)
        if scheduled is not None:
            parent_seed_id = getattr(scheduled, 'pending_parent_seed_id', '')
            scheduler = getattr(scheduled, 'scheduler', None)
            if scheduler is not None:
                active_remaining = getattr(scheduler, 'active_remaining', '')
                active_selection = getattr(scheduler, 'active_selection', None)
                if active_selection is not None:
                    selected_energy = getattr(active_selection, 'energy', '')

        if debug:
            print('[DifuzzRTL] Fuzz Instructions')
            for inst, INT in zip(sim_input.get_insts(), sim_input.ints + [0]):
                print('{:<50}{:04b}'.format(inst, INT))

        t0 = time.perf_counter()
        (isa_input, rtl_input, symbols) = preprocessor.process(sim_input, data, assert_intr)
        preprocess_s = time.perf_counter() - t0

        if isa_input and rtl_input:
            t0 = time.perf_counter()
            ret = run_isa_test(isaHost, isa_input, stop, out, proc_num)
            isa_s = time.perf_counter() - t0
            if ret == proc_state.ERR_ISA_TIMEOUT:
                if timing_path:
                    append_timing_trace(timing_path, [
                        start_iter + it, phase_at_get, parent_seed_id,
                        'isa_timeout', False, last_coverage, 0, 0, 0,
                        False, active_remaining, selected_energy,
                        get_s, preprocess_s, isa_s, rtl_s, post_s,
                        admit_s, observe_s, phase_s, log_s,
                        time.perf_counter() - iter_t0,
                    ])
                continue
            elif ret == proc_state.ERR_ISA_ASSERT: break

            try:
                t0 = time.perf_counter()
                result = yield rtlHost.run_test(
                    rtl_input, assert_intr,
                    target_bitmap_module=target_module,
                    target_bitmap_sample_period=target_bitmap_sample_period)
                rtl_s = time.perf_counter() - t0
                if len(result) == 3:
                    (ret, coverage, module_covs) = result
                else:
                    (ret, coverage) = result
                    module_covs = last_module_covs.copy()
            except:
                stop[0] = proc_state.ERR_RTL_SIM
                break

            t0 = time.perf_counter()
            if assert_intr and ret == SUCCESS and not skip_checker:
                (intr_prv, epc) = checker.check_intr(symbols)
                if epc != 0:
                    preprocessor.write_isa_intr(isa_input, rtl_input, epc)
                    ret = run_isa_test(isaHost, isa_input, stop, out, proc_num, True)
                    if ret == proc_state.ERR_ISA_TIMEOUT: continue
                    elif ret == proc_state.ERR_ISA_ASSERT: break
                else: continue

            cause = '-'
            match = False
            if ret == SUCCESS:
                match = True if skip_checker else checker.check(symbols)
            elif ret == ILL_MEM:
                match = True
                debug_print('[DifuzzRTL] Memory access outside DRAM -- {}'. \
                            format(iNum), debug, True)
                if record:
                    save_mismatch(out, proc_num, out + '/illegal',
                                  sim_input, data, iNum)
                iNum += 1

            if not match or ret not in [SUCCESS, ILL_MEM]:
                if multicore:
                    mNum = manager.read_num('mNum')
                    manager.write_num('mNum', mNum + 1)

                if record:
                    save_mismatch(out, proc_num, out + '/mismatch',
                                  sim_input, data, mNum)

                mNum += 1
                if ret == TIME_OUT: cause = 'Timeout'
                elif ret == ASSERTION_FAIL: cause = 'Assertion fail'
                else: cause = 'Mismatch'

                debug_print('[DifuzzRTL] Bug -- {} [{}]'. \
                            format(mNum, cause), debug, not match or (ret != SUCCESS))

            target_hit_bits = set(getattr(rtlHost, 'last_target_cov_hits', set()))
            target_new_bits = mutator.target_new_bits(target_hit_bits)
            add_for_target = bool(target_module and target_new_bits)
            post_s = time.perf_counter() - t0

            t0 = time.perf_counter()
            if coverage > last_coverage or add_for_target:
                if multicore:
                    cNum = manager.read_num('cNum')
                    manager.write_num('cNum', cNum + 1)

                if record:
                    t_log = time.perf_counter()
                    append_cov_log(record, cov_log, start_time,
                                   start_iter + it, start_cov + coverage,
                                   module_cov_names, module_covs)
                    log_s += time.perf_counter() - t_log
                    sim_input.save(out + '/corpus/id_{}.si'.format(cNum))

                cNum += 1
                mutator.add_corpus(sim_input, coverage, module_covs,
                                   target_hit_bits)
                if coverage > last_coverage:
                    last_coverage = coverage
                last_module_covs = module_covs.copy()
            else:
                last_module_covs = module_covs.copy()
            admit_s = time.perf_counter() - t0

            t0 = time.perf_counter()
            mutator.observe_target_yield_result(
                target_new_bits, admitted=add_for_target)
            observe_s = time.perf_counter() - t0
            t0 = time.perf_counter()
            mutator.update_phase(it)
            phase_s = time.perf_counter() - t0
            if record and not multicore and cov_log:
                t0 = time.perf_counter()
                append_cov_log(record, cov_log, start_time,
                               start_iter + it + 1, start_cov + coverage,
                               module_cov_names, module_covs)
                log_s += time.perf_counter() - t0
            if timing_path:
                target_cov = 0
                if target_module:
                    target_cov = int(module_covs.get(target_module, 0) or 0)
                append_timing_trace(timing_path, [
                    start_iter + it, phase_at_get, parent_seed_id,
                    ret, match, start_cov + coverage, target_cov,
                    len(target_hit_bits), len(target_new_bits), add_for_target,
                    active_remaining, selected_energy,
                    get_s, preprocess_s, isa_s, rtl_s, post_s,
                    admit_s, observe_s, phase_s, log_s,
                    time.perf_counter() - iter_t0,
                ])

        else:
            if timing_path:
                append_timing_trace(timing_path, [
                    start_iter + it, phase_at_get, parent_seed_id,
                    'compile_fail', False, last_coverage, 0, 0, 0,
                    False, active_remaining, selected_energy,
                    get_s, preprocess_s, isa_s, rtl_s, post_s,
                    admit_s, observe_s, phase_s, log_s,
                    time.perf_counter() - iter_t0,
                ])
            stop[0] = proc_state.ERR_COMPILE
            # Compile failed
            break

    if multicore:
        save_err(out, proc_num, manager, stop[0])
        manager.set_state(proc_num, stop[0])

    debug_print('[DifuzzRTL] Stop Fuzzing', debug)

    if multicore:
        yield manager.cov_store(dut, proc_num)
        manager.store_covmap(proc_num, start_time, start_iter, num_iter)
