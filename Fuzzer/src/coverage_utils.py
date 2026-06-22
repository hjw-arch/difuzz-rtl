from collections import OrderedDict

from fuzzer.rtl_coverage import get_cov_module_names


def ensure_target_module(module_names, target_module=None):
    names = list(module_names or [])
    if target_module and target_module not in names:
        names.append(target_module)
    return names


def get_cov_log_header(module_names):
    columns = ["time", "iter", "coverage"] + list(module_names or [])
    return "\t".join(columns) + "\n"


def get_cov_log_row(elapsed_time, iteration, total_cov, module_names,
                    module_covs=None):
    module_covs = module_covs or {}
    values = [str(elapsed_time), str(iteration), str(total_cov)]
    values.extend(str(module_covs.get(module_name, 0))
                  for module_name in (module_names or []))
    return "\t".join(values) + "\n"


def get_zero_cov_row(module_names):
    zero_covs = OrderedDict((module_name, 0) for module_name in module_names)
    return get_cov_log_row(0, 0, 0, module_names, zero_covs)


def get_cov_prefix_aggregate(module_names, cov_file_sums):
    module_covs = OrderedDict((module_name, 0) for module_name in module_names)

    for cov_file, cov_sum in cov_file_sums.items():
        prefix = cov_file.split(".", 1)[0]
        if prefix in module_covs:
            module_covs[prefix] += cov_sum

    return module_covs
