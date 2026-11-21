from __future__ import annotations

import importlib.util
import stat
import sys
from collections import Counter
from pathlib import Path

import pytest
from openpyxl import Workbook

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "sync_school_users.py"
SPEC = importlib.util.spec_from_file_location("sync_school_users", SCRIPT_PATH)
assert SPEC and SPEC.loader
sync = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sync
SPEC.loader.exec_module(sync)


def _source(
    username: str,
    name: str,
    old_class: str,
    password: str = "Strong123",
) -> object:
    return sync.SourceUser(2, username, password, name, "student", old_class)


def _target(target_id: str, name: str, student_class: str) -> object:
    return sync.RosterStudent(
        target_id, name, student_class, target_id.split(":")[0], 6
    )


def test_load_current_roster_reads_only_canonical_current_sheets(
    tmp_path: Path,
) -> None:
    workbook = Workbook()
    current = workbook.active
    current.title = "XA"
    current.append(["NO", "NISN", "NAMA SISWA"])
    current.append([1, "1234567890", "Siswa Baru"])
    stale = workbook.create_sheet("XII IIS ")
    stale.append(["NAMA SISWA"])
    stale.append(["Siswa Lama"])
    xii = workbook.create_sheet("XIIA")
    xii.append(["NAMA SISWA"])
    xii.append(["Siswa Naik"])
    path = tmp_path / "roster.xlsx"
    workbook.save(path)

    roster = sync.load_current_roster(path)

    assert [(item.full_name, item.student_class) for item in roster] == [
        ("Siswa Baru", "X A"),
        ("Siswa Naik", "XII A"),
    ]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_reconciliation_separates_exact_review_new_and_graduates() -> None:
    sources = [
        _source("lama.1001", "Nama Sama", "X A"),
        _source("beda.1002", "Nama Lama", "XI A"),
        _source("lulus.1003", "Nama Kembar", "XII A"),
        _source("lanjut.1004", "Nama Kembar", "X B"),
    ]
    roster = [
        _target("XIA:6", "Nama Sama", "XI A"),
        _target("XIIA:6", "Nama Berubah", "XII A"),
        _target("XIB:6", "Nama Kembar", "XI B"),
        _target("XA:6", "Siswa Baru", "X A"),
    ]

    plan = sync.build_reconciliation_plan([], sources, roster)

    assert len(plan.exact_matches) == 2
    assert {source.username for source, _ in plan.exact_matches} == {
        "lama.1001",
        "lanjut.1004",
    }
    assert [item.username for item in plan.review_sources] == ["beda.1002"]
    assert [item.target_id for item in plan.review_targets] == ["XIIA:6"]
    assert [item.full_name for item in plan.new_students] == ["Siswa Baru"]
    assert [item.username for item in plan.graduates] == ["lulus.1003"]


def test_old_class_normalization_supports_section_f() -> None:
    assert sync.normalize_class_name("XI F") == "XI F"
    assert sync.normalize_class_name("XI-F") == "XI F"


def test_review_mapping_requires_explicit_complete_approval(tmp_path: Path) -> None:
    source = _source("beda.1002", "Nama Lama", "XI A")
    target = _target("XIIA:6", "Nama Berubah", "XII A")
    plan = sync.ReconciliationPlan(review_sources=[source], review_targets=[target])
    rows = sync.build_review_rows(plan)
    mapping = tmp_path / "review.csv"
    sync.write_private_csv(mapping, rows, sync.REVIEW_FIELDS)

    with pytest.raises(sync.SyncError, match="incomplete"):
        sync.load_approved_mapping(mapping, plan)

    rows[0]["approved"] = "yes"
    sync.write_private_csv(mapping, rows, sync.REVIEW_FIELDS)
    resolved = sync.load_approved_mapping(mapping, plan)

    assert resolved.matches == [(source, target)]
    assert resolved.excluded_sources == []
    assert resolved.new_students == []
    assert stat.S_IMODE(mapping.stat().st_mode) == 0o600


def test_review_rows_handle_departures_and_transfer_students() -> None:
    old_x = _source("oldx.1001", "Nama Lama X", "X A")
    old_xi = _source("oldxi.1002", "Nama Lama XI", "XI A")
    target_xi_one = _target("XIA:6", "Nama Baru XI Satu", "XI A")
    target_xi_two = _target("XIA:7", "Nama Baru XI Dua", "XI A")
    plan = sync.ReconciliationPlan(
        review_sources=[old_x, old_xi],
        review_targets=[target_xi_one, target_xi_two],
    )

    rows = sync.build_review_rows(plan)

    assert Counter(row["decision"] for row in rows) == {
        "match": 1,
        "exclude": 1,
        "new": 1,
    }


def test_build_desired_users_hardens_weak_and_generates_school_username() -> None:
    teacher = sync.SourceUser(2, "guru.1000", "weakpass", "Guru", "teacher")
    source = _source("siswa.1001", "Siswa Lama", "X A", password="weakpass")
    target = _target("XIA:6", "Siswa Lama", "XI A")
    new_target = _target("XA:6", "Murid Baru", "X A")
    plan = sync.ReconciliationPlan(
        teachers=[teacher],
        exact_matches=[(source, target)],
        new_students=[new_target],
    )

    desired = sync.build_desired_users(plan, sync.ReviewResolution())

    assert len(desired) == 3
    assert all(sync.password_meets_policy(item.password) for item in desired)
    new_student = next(
        item for item in desired if item.credential_reason == "new-student"
    )
    assert new_student.username.startswith("murid.")
    assert new_student.username.rsplit(".", 1)[1].isdigit()
    assert len(new_student.username.rsplit(".", 1)[1]) == 4
    assert sync.changed_credentials(desired)

    repeated = sync.build_desired_users(plan, sync.ReviewResolution())
    repeated_new = next(
        item for item in repeated if item.credential_reason == "new-student"
    )
    assert repeated_new.username == new_student.username


def test_apply_gate_covers_review_targets_without_review_sources() -> None:
    # Transfer-in student in XI/XII: every legacy student matched by name, so
    # review_sources is empty while review_targets still holds an unmatched
    # roster row. Without a reviewed mapping that student is silently dropped.
    sources = [_source("lama.1001", "Nama Sama", "X A")]
    roster = [
        _target("XIA:6", "Nama Sama", "XI A"),
        _target("XIA:7", "Murid Pindahan", "XI A"),
    ]

    plan = sync.build_reconciliation_plan([], sources, roster)

    assert plan.review_sources == []
    assert [item.target_id for item in plan.review_targets] == ["XIA:7"]

    summary = sync.build_summary(plan)
    desired = sync.build_desired_users(plan, sync.ReviewResolution())

    # The summary promises the transfer-in student, so an unreviewed apply must
    # be blocked rather than writing fewer accounts than reported.
    assert summary["students"]["final_total"] == 2
    assert len(desired) == 1
    assert bool(plan.review_sources or plan.review_targets) is True


def test_new_student_username_never_reuses_a_graduate_account() -> None:
    new_target = _target("XA:6", "Murid Baru", "X A")
    plan_without = sync.ReconciliationPlan(new_students=[new_target])
    generated = sync.build_desired_users(plan_without, sync.ReviewResolution())[0]

    assert generated.is_new_account is True

    # A graduate holding the username the generator would pick must reserve it,
    # otherwise apply_users would update that account in place and rebind the
    # leaver's exam history to a different person.
    graduate = _source(generated.username, "Alumni Lain", "XII A")
    plan_with = sync.ReconciliationPlan(
        new_students=[new_target],
        graduates=[graduate],
    )

    reserved = sync.build_desired_users(plan_with, sync.ReviewResolution())[0]

    assert reserved.username != generated.username
    assert reserved.is_new_account is True


def test_excluded_review_sources_also_reserve_their_username() -> None:
    new_target = _target("XA:6", "Murid Baru", "X A")
    baseline = sync.build_desired_users(
        sync.ReconciliationPlan(new_students=[new_target]),
        sync.ReviewResolution(),
    )[0]
    departed = _source(baseline.username, "Siswa Keluar", "X B")

    resolution = sync.ReviewResolution(excluded_sources=[departed])
    reserved = sync.build_desired_users(
        sync.ReconciliationPlan(new_students=[new_target]),
        resolution,
    )[0]

    assert reserved.username != baseline.username


def test_matched_students_are_not_flagged_as_new_accounts() -> None:
    source = _source("siswa.1001", "Siswa Lama", "X A")
    target = _target("XIA:6", "Siswa Lama", "XI A")
    plan = sync.ReconciliationPlan(exact_matches=[(source, target)])

    desired = sync.build_desired_users(plan, sync.ReviewResolution())

    assert [item.is_new_account for item in desired] == [False]


def test_parse_args_requires_explicit_apply_confirmations(tmp_path: Path) -> None:
    args = sync.parse_args(
        [
            "--csv",
            str(tmp_path / "users.csv"),
            "--excel",
            str(tmp_path / "roster.xlsx"),
            "--report-dir",
            str(tmp_path / "reports"),
            "--apply",
        ]
    )
    with pytest.raises(sync.SyncError, match="requires"):
        __import__("asyncio").run(sync.async_main(args))
