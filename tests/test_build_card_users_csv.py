from __future__ import annotations

import importlib.util
import stat
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "build_card_users_csv.py"
)
SPEC = importlib.util.spec_from_file_location("build_card_users_csv", SCRIPT_PATH)
assert SPEC and SPEC.loader
cards = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = cards
SPEC.loader.exec_module(cards)


def test_build_card_rows_matches_generator_contract() -> None:
    users = [
        {
            "username": "guru.1000",
            "full_name": "Nama Guru",
            "role": "teacher",
            "student_class": "",
            "job_title": "Guru Matematika",
            "is_active": "t",
            "joined_date": "2026-08-25",
        },
        {
            "username": "siswa.1001",
            "full_name": "Nama Siswa",
            "role": "student",
            "student_class": "X A",
            "job_title": "",
            "is_active": "t",
            "joined_date": "2026-08-25",
        },
    ]

    rows = cards.build_card_rows(
        users,
        {"guru.1000": "Teacher123", "siswa.1001": "Student123"},
    )

    assert list(rows[0]) == list(cards.OUTPUT_FIELDS)
    assert rows[0]["Role"] == "guru"
    assert rows[0]["Kelas/Jabatan"] == "Guru Matematika"
    assert rows[1]["Role"] == "siswa"
    assert rows[1]["Kelas/Jabatan"] == "X A"


def test_build_card_rows_requires_password_for_every_account() -> None:
    users = [
        {
            "username": "siswa.1001",
            "full_name": "Nama Siswa",
            "role": "student",
            "student_class": "X A",
            "job_title": "",
            "is_active": "t",
            "joined_date": "2026-08-25",
        }
    ]

    with pytest.raises(cards.CardExportError, match="no recoverable"):
        cards.build_card_rows(users, {})


def test_write_private_csv_uses_mode_600(tmp_path: Path) -> None:
    output = tmp_path / "private" / "cards.csv"
    rows = [
        {
            "No": "1",
            "Role": "siswa",
            "Nama Lengkap": "Nama Siswa",
            "Username": "siswa.1001",
            "Kelas/Jabatan": "X A",
            "Password": "Student123",
            "Status": "Aktif",
            "Bergabung": "2026-08-25",
        }
    ]

    cards.write_private_csv(output, rows)

    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert output.read_bytes().startswith(b"\xef\xbb\xbf")
    # The script created this directory, so tightening it is correct.
    assert stat.S_IMODE(output.parent.stat().st_mode) == 0o700


def test_write_private_csv_leaves_an_existing_parent_mode_alone(
    tmp_path: Path,
) -> None:
    # Writing into a pre-existing directory must not narrow it: --output ./x.csv
    # would otherwise chmod the repository root to 0700.
    parent = tmp_path / "existing"
    parent.mkdir(mode=0o755)
    before = stat.S_IMODE(parent.stat().st_mode)
    rows = [
        {
            "No": "1",
            "Role": "siswa",
            "Nama Lengkap": "Nama Siswa",
            "Username": "siswa.1001",
            "Kelas/Jabatan": "X A",
            "Password": "Student123",
            "Status": "Aktif",
            "Bergabung": "2026-08-25",
        }
    ]

    cards.write_private_csv(parent / "cards.csv", rows)

    assert stat.S_IMODE(parent.stat().st_mode) == before
    assert stat.S_IMODE((parent / "cards.csv").stat().st_mode) == 0o600


def test_validate_counts_accepts_a_zero_role_expectation() -> None:
    rows = [{"Role": "guru"}, {"Role": "guru"}]

    cards.validate_counts(rows, expected_teachers=2, expected_students=0)

    with pytest.raises(cards.CardExportError, match="role counts"):
        cards.validate_counts(rows, expected_teachers=2, expected_students=1)
