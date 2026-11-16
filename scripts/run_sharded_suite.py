#!/usr/bin/env python3
"""Run the whole backend suite as many small pytest processes, each collecting only its part.

One ``pytest -n N`` run makes every xdist worker collect every test (~280k items) before it
runs one, so its memory grows with N and a single controller handles every report. Here
``scripts/ci_file_shards.py`` assigns whole files to ``--shards`` parts before import, and
at most ``--jobs`` parts run at once. Peak memory is about ``jobs`` processes of
``1/shards`` of the suite; more shards at the same jobs means less memory and better
balance. Every file still runs exactly once.

Parts run side by side, so the files of one ``xdist_group`` are pinned to one part: under
``--dist loadgroup`` a group runs on one worker, which is how ``serial`` and
``subprocess_spawn`` keep their tests from contending on the host, and two of its files in
two parts would run at once.

    python3 scripts/run_sharded_suite.py                    # sized from this machine
    python3 scripts/run_sharded_suite.py --jobs 4 --shards 32
    python3 scripts/run_sharded_suite.py -- -m "not ipv6_required and not real_venv"

Arguments after ``--`` go to every pytest process. Each part writes ``junit-<i>.xml`` and
``shard-<i>.log`` under ``--out``. Exit status is 0 only when every part passed (a part
that collected nothing counts as passed: its files belong to other parts; a run in which no
part collected anything exits 5, as pytest does).
"""

from __future__ import annotations

import argparse
import concurrent.futures
import configparser
import math
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from defusedxml.ElementTree import ParseError, parse  # noqa: E402

from kiro_crew import platform_compat  # noqa: E402
from scripts.ci_file_shards import hash_shard  # noqa: E402

#: A literal ``xdist_group`` name. shortcut: a name built at runtime is not seen, so its
#: files keep their hash owners; read the marks from a collection if one ever spans files.
_GROUP_MARK = re.compile(r"""xdist_group\(\s*(?:name\s*=\s*)?["']([^"']+)["']""")

#: Per-process peak memory, GiB, measured on the full Linux suite at ``-n0`` (r7i.8xlarge):
#: ``FIXED + SUITE / shards``. 8 parts peaked at 3.5 GiB each, 32 parts at 1.06 GiB; all 32
#: parts at once peaked at 33.6 GiB on the whole machine, where ``pytest -n 32`` took 120 GiB.
#: Re-measure when the suite grows by half again.
PROCESS_FIXED_GIB = 0.25
SUITE_GIB = 27.0
#: pytest's own "no tests collected" exit status.
NO_TESTS_COLLECTED = 5


def available_gib() -> float | None:
    """Memory free for new processes, in GiB; ``None`` when the platform cannot say."""
    mib = platform_compat.host_available_mib()
    return mib / 1024 if mib else None


def plan(jobs: int, available: float | None, headroom: float = 0.8) -> int:
    """The fewest shards whose ``jobs`` concurrent parts fit in ``headroom`` of memory.

    Never fewer shards than jobs (an idle job slot buys nothing). With no memory reading,
    four parts per job.
    """
    if available is None:
        return jobs * 4
    budget = available * headroom - jobs * PROCESS_FIXED_GIB
    if budget <= 0:
        raise SystemExit(
            f"{jobs} parallel parts do not fit in {available:.1f} GiB available; pass a "
            "smaller --jobs"
        )
    return max(jobs, math.ceil(SUITE_GIB * jobs / budget))


def testpaths(root: Path) -> list[Path]:
    """``[tool:pytest] testpaths`` from ``setup.cfg``."""
    config = configparser.RawConfigParser()
    config.read(root / "setup.cfg", encoding="utf-8")
    return [root / name for name in config.get("tool:pytest", "testpaths").split()]


def group_pins(root: Path, shards: int) -> dict[str, int]:
    """One part for every file of each ``xdist_group`` that spans files.

    Files joined through any shared group form one set, owned by the hash owner of its
    first path, so the answer depends only on the tree and the part count.
    """
    parent: dict[str, str] = {}

    def find(node: str) -> str:
        while parent.setdefault(node, node) != node:
            node = parent[node]
        return node

    for top in testpaths(root):
        for path in sorted({*top.rglob("test_*.py"), *top.rglob("*_test.py")}):
            relative = path.relative_to(root).as_posix()
            for name in set(_GROUP_MARK.findall(path.read_text(encoding="utf-8"))):
                parent[find(relative)] = find("group:" + name)
    members: dict[str, list[str]] = {}
    for node in parent:
        if not node.startswith("group:"):
            members.setdefault(find(node), []).append(node)
    pins = {}
    for files in members.values():
        if len(files) > 1:
            owner = hash_shard(min(files), shards)
            pins.update(dict.fromkeys(files, owner))
    return pins


def write_pins(pins: dict[str, int], shards: int, path: Path) -> Path:
    """``pins`` in ``scripts/ci_file_shards.py``'s ``--file-shard-pins`` format."""
    lines = [f"# shards: {shards}", *(f"{owner} {name}" for name, owner in sorted(pins.items()))]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def part_command(
    index: int, shards: int, workers: int, out: Path, extra: list[str], pins: Path
) -> list[str]:
    return [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "scripts.ci_file_shards",
        "--file-shards",
        str(shards),
        "--file-shard",
        str(index),
        f"--file-shard-pins={pins}",
        "-n",
        str(workers),
        f"--junitxml={out / f'junit-{index}.xml'}",
        *extra,
    ]


def junit_counts(path: Path) -> tuple[int, int]:
    """``(tests, failures + errors)`` in one part's JUnit file; zeros when it is absent."""
    try:
        root = parse(path).getroot()
    except (OSError, ParseError):
        return 0, 0
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    tests = sum(int(s.get("tests", 0)) for s in suites)
    bad = sum(int(s.get("failures", 0)) + int(s.get("errors", 0)) for s in suites)
    return tests, bad


def run_part(
    index: int, shards: int, workers: int, out: Path, extra: list[str], pins: Path
) -> tuple:
    started = time.monotonic()
    with open(out / f"shard-{index}.log", "wb") as log:
        rc = subprocess.call(
            part_command(index, shards, workers, out, extra, pins),
            cwd=ROOT,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    return index, rc, time.monotonic() - started


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    extra: list[str] = []
    if "--" in args:
        cut = args.index("--")
        args, extra = args[:cut], args[cut + 1 :]
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 1, help="parts at once")
    parser.add_argument("--shards", type=int, default=None, help="parts in total")
    parser.add_argument("--workers", type=int, default=0, help="xdist workers per part")
    parser.add_argument("--out", type=Path, default=None, help="directory for logs and JUnit")
    opts = parser.parse_args(args)
    jobs = max(1, opts.jobs)
    shards = opts.shards or plan(jobs, available_gib())
    # Absolute: every part runs from the repository root, not from where this was started.
    out = (opts.out or Path(tempfile.mkdtemp(prefix="kc-sharded-suite-"))).resolve()
    out.mkdir(parents=True, exist_ok=True)
    pins = group_pins(ROOT, shards)
    pin_file = write_pins(pins, shards, out / "group-pins.txt")
    print(
        f"{shards} parts, {jobs} at a time, -n {opts.workers} each, {len(pins)} grouped files "
        f"pinned; logs in {out}",
        flush=True,
    )

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = [
            pool.submit(run_part, i, shards, opts.workers, out, extra, pin_file)
            for i in range(1, shards + 1)
        ]
        for future in concurrent.futures.as_completed(futures):
            index, rc, secs = future.result()
            tests, bad = junit_counts(out / f"junit-{index}.xml")
            results.append((index, rc, secs, tests, bad))
            print(f"part {index}/{shards}: exit {rc}, {tests} tests, {bad} failed, {secs:.0f}s")

    failed = [r for r in results if r[1] not in (0, NO_TESTS_COLLECTED)]
    total = sum(r[3] for r in results)
    bad = sum(r[4] for r in results)
    print(f"{total} tests in {shards} parts, {bad} failed or errored; {len(failed)} part(s) red")
    for index, rc, _secs, _tests, _bad in sorted(failed):
        print(f"  red: part {index} (exit {rc}), see {out / f'shard-{index}.log'}")
    if all(r[1] == NO_TESTS_COLLECTED for r in results):
        # A selection that matches nothing (a misspelt -m) must not read as green.
        print("  red: no part collected a test")
        return NO_TESTS_COLLECTED
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
