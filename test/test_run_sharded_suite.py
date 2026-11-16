"""``scripts/run_sharded_suite.py`` sizes its parts from memory and reports every red part."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import run_sharded_suite as runner


def test_the_plan_fits_the_parts_in_the_memory_budget() -> None:
    shards = runner.plan(jobs=8, available=16.0)

    per_part = runner.PROCESS_FIXED_GIB + runner.SUITE_GIB / shards
    assert 8 * per_part <= 16.0 * 0.8


def test_the_plan_never_has_fewer_parts_than_jobs() -> None:
    assert runner.plan(jobs=32, available=10_000.0) == 32


def test_an_unknown_memory_reading_gives_four_parts_per_job() -> None:
    assert runner.plan(jobs=3, available=None) == 12


def test_jobs_that_cannot_fit_at_all_are_refused() -> None:
    with pytest.raises(SystemExit, match="smaller --jobs"):
        runner.plan(jobs=64, available=10.0)


def test_each_part_collects_only_its_own_files(tmp_path: Path) -> None:
    command = runner.part_command(3, 16, 0, tmp_path, ["-m", "not slow"], tmp_path / "p.txt")

    assert command[1:5] == ["-m", "pytest", "-p", "scripts.ci_file_shards"]
    assert command[5:9] == ["--file-shards", "16", "--file-shard", "3"]
    assert command[9] == f"--file-shard-pins={tmp_path / 'p.txt'}"
    assert command[10:12] == ["-n", "0"]
    assert f"--junitxml={tmp_path / 'junit-3.xml'}" in command
    assert command[-2:] == ["-m", "not slow"]


def test_junit_counts_read_failures_and_errors(tmp_path: Path) -> None:
    (tmp_path / "junit-1.xml").write_text(
        '<testsuites><testsuite tests="5" failures="1" errors="2"/></testsuites>',
        encoding="utf-8",
    )

    assert runner.junit_counts(tmp_path / "junit-1.xml") == (5, 3)
    assert runner.junit_counts(tmp_path / "missing.xml") == (0, 0)


def _run_with(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, codes: dict[int, int]) -> int:
    monkeypatch.setattr(runner, "run_part", lambda index, *_args: (index, codes.get(index, 0), 0.0))
    # The real tree's groups are not what these tests are about; keep them off disk.
    monkeypatch.setattr(runner, "group_pins", lambda _root, _shards: {})
    return runner.main(["--jobs", "2", "--shards", str(len(codes)), "--out", str(tmp_path)])


def test_a_relative_out_is_resolved_before_the_parts_change_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Every part runs from the repository root, so a relative pin path would miss."""
    seen: list[tuple[Path, Path]] = []

    def part(index, _shards, _workers, out, _extra, pins):
        seen.append((out, pins))
        return index, 0, 0.0

    monkeypatch.setattr(runner, "run_part", part)
    monkeypatch.setattr(runner, "group_pins", lambda _root, _shards: {})
    monkeypatch.chdir(tmp_path)

    assert runner.main(["--jobs", "1", "--shards", "1", "--out", "results"]) == 0

    ((out, pins),) = seen
    assert out == tmp_path / "results" and out.is_absolute()
    assert pins == out / "group-pins.txt" and pins.is_file()


def test_a_part_that_collected_nothing_is_not_red(monkeypatch, tmp_path: Path) -> None:
    assert _run_with(monkeypatch, tmp_path, {1: 0, 2: runner.NO_TESTS_COLLECTED}) == 0


def test_a_run_where_no_part_collected_anything_is_red(monkeypatch, tmp_path: Path) -> None:
    codes = {1: runner.NO_TESTS_COLLECTED, 2: runner.NO_TESTS_COLLECTED}

    assert _run_with(monkeypatch, tmp_path, codes) == runner.NO_TESTS_COLLECTED


def test_one_red_part_makes_the_run_red(monkeypatch, tmp_path: Path, capsys) -> None:
    assert _run_with(monkeypatch, tmp_path, {1: 0, 2: 1, 3: 0}) == 1
    assert "red: part 2 (exit 1)" in capsys.readouterr().out


def _tree(root: Path, files: dict[str, str]) -> Path:
    (root / "setup.cfg").write_text("[tool:pytest]\ntestpaths = test pkg\n", encoding="utf-8")
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
    return root


def test_files_joined_by_a_group_share_one_part_and_a_lone_group_file_is_not_pinned(
    tmp_path: Path,
) -> None:
    root = _tree(
        tmp_path,
        {
            "test/test_a.py": 'pytestmark = pytest.mark.xdist_group(name="serial")\n',
            "test/test_b.py": "@pytest.mark.xdist_group('serial')\n"
            '@pytest.mark.xdist_group("spawn")\n',
            "pkg/x/tests/test_c.py": '@pytest.mark.xdist_group( name = "spawn" )\n',
            "test/test_alone.py": 'pytestmark = pytest.mark.xdist_group("only_here")\n',
            "test/test_plain.py": "def test_x(): pass\n",
        },
    )

    pins = runner.group_pins(root, 32)

    # a and c share no group, but b joins them, so all three run in one part
    assert set(pins) == {"test/test_a.py", "test/test_b.py", "pkg/x/tests/test_c.py"}
    assert len(set(pins.values())) == 1
    assert runner.group_pins(root, 32) == pins


def test_the_pin_file_is_what_ci_file_shards_reads(tmp_path: Path) -> None:
    from scripts import ci_file_shards

    path = runner.write_pins({"test/test_a.py": 3, "test/test_b.py": 3}, 4, tmp_path / "p.txt")

    assert ci_file_shards.read_pins(path, 4) == {"test/test_a.py": 3, "test/test_b.py": 3}


def test_every_part_reads_the_group_pins(tmp_path: Path) -> None:
    command = runner.part_command(1, 4, 0, tmp_path, [], tmp_path / "p.txt")

    assert f"--file-shard-pins={tmp_path / 'p.txt'}" in command
