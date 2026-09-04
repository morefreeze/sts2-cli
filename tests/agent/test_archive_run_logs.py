import os

import pytest

import agent.archive_run_logs as archive_run_logs
from agent.archive_run_logs import main


def _write_log(log_dir, name, *, content=b"x", mtime=None):
    path = log_dir / name
    path.write_bytes(content)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _names(directory):
    if not directory.exists():
        return set()
    return {p.name for p in directory.iterdir() if p.is_file()}


def test_keeps_newest_n_per_day_and_archives_the_rest_into_per_day_dirs(tmp_path):
    log_dir = tmp_path / "logs"
    archive_dir = tmp_path / "archive"
    log_dir.mkdir()

    # 5 runs on the same day, distinct timestamps embedded in the filename.
    names = [
        "20260810_090000_Ironclad_a.jsonl",
        "20260810_100000_Ironclad_b.jsonl",
        "20260810_110000_Ironclad_c.jsonl",
        "20260810_120000_Ironclad_d.jsonl",
        "20260810_130000_Ironclad_e.jsonl",
    ]
    for n in names:
        _write_log(log_dir, n)

    exit_code = main(
        [
            "--keep",
            "3",
            "--log-dir",
            str(log_dir),
            "--archive-dir",
            str(archive_dir),
            "--apply",
        ]
    )

    assert exit_code == 0
    # Newest 3 (by HHMMSS) remain in logs/.
    assert _names(log_dir) == {
        "20260810_130000_Ironclad_e.jsonl",
        "20260810_120000_Ironclad_d.jsonl",
        "20260810_110000_Ironclad_c.jsonl",
    }
    # Oldest 2 moved into the per-day archive subdirectory.
    day_dir = archive_dir / "2026-08-10"
    assert _names(day_dir) == {
        "20260810_100000_Ironclad_b.jsonl",
        "20260810_090000_Ironclad_a.jsonl",
    }


def test_day_with_fewer_than_keep_files_is_left_untouched(tmp_path):
    log_dir = tmp_path / "logs"
    archive_dir = tmp_path / "archive"
    log_dir.mkdir()
    names = [
        "20260811_090000_Silent_a.jsonl",
        "20260811_100000_Silent_b.jsonl",
    ]
    for n in names:
        _write_log(log_dir, n)

    exit_code = main(
        [
            "--keep",
            "30",
            "--log-dir",
            str(log_dir),
            "--archive-dir",
            str(archive_dir),
            "--apply",
        ]
    )

    assert exit_code == 0
    assert _names(log_dir) == set(names)
    assert not (archive_dir / "2026-08-11").exists()


def test_dry_run_moves_nothing_and_reports_the_same_plan_as_apply(tmp_path, capsys):
    def make_logs(base):
        base.mkdir(parents=True)
        for n in [
            "20260812_090000_Defect_a.jsonl",
            "20260812_100000_Defect_b.jsonl",
            "20260812_110000_Defect_c.jsonl",
        ]:
            _write_log(base, n, content=b"same-content-1234")
        return base

    # Two identical, independent fixtures: one previewed, one applied.
    dry_log_dir = make_logs(tmp_path / "dry" / "logs")
    dry_archive_dir = tmp_path / "dry" / "archive"
    apply_log_dir = make_logs(tmp_path / "apply" / "logs")
    apply_archive_dir = tmp_path / "apply" / "archive"

    dry_exit = main(
        ["--keep", "1", "--log-dir", str(dry_log_dir), "--archive-dir", str(dry_archive_dir)]
    )
    dry_output = capsys.readouterr().out

    apply_exit = main(
        [
            "--keep",
            "1",
            "--log-dir",
            str(apply_log_dir),
            "--archive-dir",
            str(apply_archive_dir),
            "--apply",
        ]
    )
    apply_output = capsys.readouterr().out

    assert dry_exit == 0
    assert apply_exit == 0
    # Dry run touched nothing.
    assert len(_names(dry_log_dir)) == 3
    assert not dry_archive_dir.exists()
    # Apply actually moved the excess files.
    assert len(_names(apply_log_dir)) == 1
    assert len(_names(apply_archive_dir / "2026-08-12")) == 2

    def strip_header(report):
        return report.splitlines()[1:]

    # Same fixture, same --keep -> identical table (day/total/kept/
    # archived/bytes), regardless of whether it was a preview or applied.
    assert strip_header(dry_output) == strip_header(apply_output)
    assert "DRY RUN" in dry_output
    assert "APPLIED" in apply_output


def test_second_apply_run_is_a_no_op(tmp_path):
    log_dir = tmp_path / "logs"
    archive_dir = tmp_path / "archive"
    log_dir.mkdir()
    names = [
        "20260813_090000_Regent_a.jsonl",
        "20260813_100000_Regent_b.jsonl",
        "20260813_110000_Regent_c.jsonl",
    ]
    for n in names:
        _write_log(log_dir, n)

    args = [
        "--keep",
        "2",
        "--log-dir",
        str(log_dir),
        "--archive-dir",
        str(archive_dir),
        "--apply",
    ]
    main(args)
    kept_after_first = _names(log_dir)
    archived_after_first = _names(archive_dir / "2026-08-13")

    exit_code = main(args)

    assert exit_code == 0
    assert _names(log_dir) == kept_after_first
    assert _names(archive_dir / "2026-08-13") == archived_after_first
    plan = archive_run_logs.plan_archive(log_dir, keep=2)
    _, to_archive = plan[archive_run_logs.date(2026, 8, 13)]
    assert to_archive == []


def test_filename_timestamp_grouping_wins_over_contradicting_mtime(tmp_path):
    log_dir = tmp_path / "logs"
    archive_dir = tmp_path / "archive"
    log_dir.mkdir()

    import datetime as _dt

    # Filename says 2026-08-14 and is the "newer" one by name, but its
    # mtime is set to a much earlier date. Grouping/order must follow the
    # filename, not the mtime.
    newer_by_name = _write_log(
        log_dir,
        "20260814_150000_Necrobinder_new.jsonl",
        mtime=_dt.datetime(2020, 1, 1).timestamp(),
    )
    older_by_name = _write_log(
        log_dir,
        "20260814_090000_Necrobinder_old.jsonl",
        mtime=_dt.datetime(2030, 1, 1).timestamp(),
    )

    exit_code = main(
        [
            "--keep",
            "1",
            "--log-dir",
            str(log_dir),
            "--archive-dir",
            str(archive_dir),
            "--apply",
        ]
    )

    assert exit_code == 0
    # Both files grouped under the filename's day (2026-08-14), not 2020
    # or 2030 from their mtimes.
    assert not (archive_dir / "2020-01-01").exists()
    assert not (archive_dir / "2030-01-01").exists()
    # The one kept is the one that's "newer" by filename timestamp
    # (15:00:00), even though its mtime is the oldest of the two.
    assert _names(log_dir) == {"20260814_150000_Necrobinder_new.jsonl"}
    assert _names(archive_dir / "2026-08-14") == {
        "20260814_090000_Necrobinder_old.jsonl"
    }


def test_destination_collision_does_not_lose_data(tmp_path):
    log_dir = tmp_path / "logs"
    archive_dir = tmp_path / "archive"
    log_dir.mkdir()
    day_dir = archive_dir / "2026-08-15"
    day_dir.mkdir(parents=True)

    colliding_name = "20260815_090000_Ironclad_a.jsonl"
    # Pre-existing archived file with the same name but different content
    # (e.g. left over from a previous manual copy).
    (day_dir / colliding_name).write_bytes(b"already-archived-content")

    _write_log(log_dir, colliding_name, content=b"new-source-content")
    _write_log(log_dir, "20260815_100000_Ironclad_b.jsonl", content=b"b")

    exit_code = main(
        [
            "--keep",
            "1",
            "--log-dir",
            str(log_dir),
            "--archive-dir",
            str(archive_dir),
            "--apply",
        ]
    )

    assert exit_code == 0
    # The pre-existing archived file must be untouched.
    assert (day_dir / colliding_name).read_bytes() == b"already-archived-content"
    # The newly archived file must exist somewhere under day_dir with its
    # own content preserved, under a disambiguated name.
    other_files = [p for p in day_dir.iterdir() if p.name != colliding_name]
    assert len(other_files) == 1
    assert other_files[0].read_bytes() == b"new-source-content"
    assert other_files[0].name != colliding_name


def test_non_jsonl_files_and_subdirectories_are_ignored(tmp_path):
    log_dir = tmp_path / "logs"
    archive_dir = tmp_path / "archive"
    log_dir.mkdir()

    # Non-jsonl sibling that happens to match the naming convention.
    _write_log(log_dir, "20260816_090000_Ironclad_a.json", content=b"not-jsonl")
    # A subdirectory containing jsonl files — must not be recursed into.
    nested = log_dir / "already_archived"
    nested.mkdir()
    _write_log(nested, "20260816_090000_Ironclad_nested.jsonl", content=b"nested")

    # Only one real top-level jsonl file, well under --keep.
    _write_log(log_dir, "20260816_100000_Ironclad_b.jsonl", content=b"real")

    exit_code = main(
        [
            "--keep",
            "1",
            "--log-dir",
            str(log_dir),
            "--archive-dir",
            str(archive_dir),
            "--apply",
        ]
    )

    assert exit_code == 0
    # Nothing archived: only one qualifying file existed, under keep=1.
    assert not archive_dir.exists()
    # The non-.jsonl file and the nested directory are untouched.
    assert (log_dir / "20260816_090000_Ironclad_a.json").exists()
    assert (nested / "20260816_090000_Ironclad_nested.jsonl").exists()
    assert (log_dir / "20260816_100000_Ironclad_b.jsonl").exists()


@pytest.mark.parametrize("keep", [-1, -5])
def test_negative_keep_is_rejected(tmp_path, keep, capsys):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()

    with pytest.raises(SystemExit):
        main(["--keep", str(keep), "--log-dir", str(log_dir)])

    assert "--keep" in capsys.readouterr().err
