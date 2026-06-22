#!/usr/bin/env python3
"""Create slang-friendly clean copies of difuzz-instrumented generated Verilog.

The transform is intentionally narrow:
  * remove the debug_print / plusargs / fwrite prefix injected at the start of
    the top module body;
  * remove the immediately following MULTICORE coverage save/restore prefix;
  * rewrite slang-yosys' unsupported reduction-or-of-not spelling, |x, to the
    equivalent reduction-nand spelling, ~&x.

Original *_state.v inputs are never modified.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path


MODULE_RE = re.compile(r"^\s*module\s+([A-Za-z_][A-Za-z0-9_$]*)\s*\(", re.MULTILINE)
PREPROC_OPEN_RE = re.compile(r"^\s*`(?:ifdef|ifndef|if)\b")
PREPROC_CLOSE_RE = re.compile(r"^\s*`endif\b")


@dataclass(frozen=True)
class StripStats:
    source: Path
    output: Path
    top_module: str
    debug_lines_removed: int
    multicore_lines_removed: int
    reduction_or_not_rewrites: int


def _default_output_path(source: Path, output_dir: Path | None) -> Path:
    stem = source.stem
    if stem.endswith("_state"):
        name = f"{stem[:-6]}_clean{source.suffix}"
    else:
        name = f"{stem}_clean{source.suffix}"
    return (output_dir if output_dir is not None else source.parent) / name


def _find_first_module(lines: list[str]) -> tuple[str, int]:
    text = "".join(lines)
    match = MODULE_RE.search(text)
    if not match:
        raise ValueError("no module declaration found")
    line_index = text[: match.start()].count("\n")
    return match.group(1), line_index


def _find_port_list_end(lines: list[str], module_line: int) -> int:
    for index in range(module_line + 1, len(lines)):
        if lines[index].strip() == ");":
            return index
    raise ValueError("could not find end of first module port list")


def _skip_blank(lines: list[str], index: int) -> int:
    while index < len(lines) and not lines[index].strip():
        index += 1
    return index


def _find_next_top_multicore(lines: list[str], start: int) -> int:
    for index in range(start, min(start + 300, len(lines))):
        if lines[index].strip() == "`ifdef MULTICORE":
            return index
    raise ValueError("could not find top-module MULTICORE coverage block after debug block")


def _find_matching_endif(lines: list[str], start: int) -> int:
    depth = 0
    for index in range(start, len(lines)):
        line = lines[index]
        if PREPROC_OPEN_RE.match(line):
            depth += 1
        elif PREPROC_CLOSE_RE.match(line):
            depth -= 1
            if depth == 0:
                return index
    raise ValueError("unterminated preprocessor block")


def _strip_top_instrumentation(lines: list[str]) -> tuple[list[str], str, int, int]:
    top_module, module_line = _find_first_module(lines)
    port_end = _find_port_list_end(lines, module_line)

    debug_start = _skip_blank(lines, port_end + 1)
    if debug_start >= len(lines) or "reg debug_print" not in lines[debug_start]:
        raise ValueError(
            f"{top_module}: expected difuzz debug_print block immediately after top-module port list"
        )

    multicore_start = _find_next_top_multicore(lines, debug_start)
    debug_chunk = lines[debug_start:multicore_start]
    if "$value$plusargs" not in "".join(debug_chunk) or "$fwrite" not in "".join(debug_chunk):
        raise ValueError(f"{top_module}: debug_print block did not contain expected simulation calls")

    multicore_end = _find_matching_endif(lines, multicore_start)
    multicore_chunk = lines[multicore_start : multicore_end + 1]
    multicore_text = "".join(multicore_chunk)
    if "cov_restore" not in multicore_text or "cov_store" not in multicore_text:
        raise ValueError(f"{top_module}: MULTICORE block did not look like coverage save/restore")
    if "$fopen" not in multicore_text or "$fwrite" not in multicore_text:
        raise ValueError(f"{top_module}: MULTICORE coverage block did not contain expected file I/O")

    stripped = lines[:debug_start] + lines[multicore_end + 1 :]
    return stripped, top_module, len(debug_chunk), len(multicore_chunk)


def clean_text(text: str) -> tuple[str, str, int, int, int]:
    lines = text.splitlines(keepends=True)
    stripped_lines, top_module, debug_removed, multicore_removed = _strip_top_instrumentation(lines)
    stripped_text = "".join(stripped_lines)
    rewrite_count = stripped_text.count("|~")
    stripped_text = stripped_text.replace("|~", "~&")
    return stripped_text, top_module, debug_removed, multicore_removed, rewrite_count


def clean_file(source: Path, output_dir: Path | None) -> StripStats:
    if not source.exists():
        raise FileNotFoundError(source)
    output = _default_output_path(source, output_dir)
    output.parent.mkdir(parents=True, exist_ok=True)

    text = source.read_text()
    cleaned, top_module, debug_removed, multicore_removed, rewrite_count = clean_text(text)
    output.write_text(cleaned)

    return StripStats(
        source=source,
        output=output,
        top_module=top_module,
        debug_lines_removed=debug_removed,
        multicore_lines_removed=multicore_removed,
        reduction_or_not_rewrites=rewrite_count,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sources", nargs="+", type=Path, help="input *_state.v file(s)")
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=None,
        help="directory for generated *_clean.v copies; defaults to each source directory",
    )
    args = parser.parse_args()

    for source in args.sources:
        stats = clean_file(source, args.output_dir)
        print(
            f"{stats.source} -> {stats.output} "
            f"(top={stats.top_module}, debug_removed={stats.debug_lines_removed}, "
            f"multicore_removed={stats.multicore_lines_removed}, "
            f"or_not_rewrites={stats.reduction_or_not_rewrites})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
