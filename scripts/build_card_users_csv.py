#!/usr/bin/env python3
"""Build a private ID-card CSV from the final user roster and credential sources."""

from __future__ import annotations

import argparse
import csv
import stat
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

OUTPUT_FIELDS = (
    "No",
    "Role",
    "Nama Lengkap",
    "Username",
    "Kelas/Jabatan",
    "Password",
    "Status",
    "Bergabung",
)


class CardExportError(RuntimeError):
    """Raised when a complete, generator-compatible export cannot be built."""


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise CardExportError(f"required CSV not found: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _normalized_headers(row: dict[str, str]) -> dict[str, str]:
    return {str(key or "").strip().casefold(): key for key in row}


def load_legacy_passwords(path: Path) -> dict[str, str]:
    rows = _read_csv(path)
    if not rows:
        raise CardExportError("legacy credential CSV is empty")
    headers = _normalized_headers(rows[0])
    username_header = headers.get("username")
    password_header = headers.get("password")
    if not username_header or not password_header:
        raise CardExportError("legacy credential CSV lacks username/password columns")
    passwords: dict[str, str] = {}
    for row in rows:
        username = str(row.get(username_header) or "").strip()
        password = str(row.get(password_header) or "").strip()
        if username and password:
            passwords[username.casefold()] = password
    return passwords


def load_changed_passwords(path: Path) -> dict[str, str]:
    rows = _read_csv(path)
    passwords: dict[str, str] = {}
    for row in rows:
        username = str(row.get("username") or "").strip()
        password = str(row.get("password") or "").strip()
        if not username or not password:
            raise CardExportError("changed credential CSV contains an incomplete row")
        key = username.casefold()
        if key in passwords:
            raise CardExportError(
                "changed credential CSV contains a duplicate username"
            )
        passwords[key] = password
    return passwords


def class_sort_key(value: str) -> tuple[int, str]:
    normalized = " ".join(str(value or "").strip().upper().split())
    grade = normalized.split()[0] if normalized else ""
    return ({"X": 10, "XI": 11, "XII": 12}.get(grade, 99), normalized)


def build_card_rows(
    users: Sequence[dict[str, str]],
    passwords: dict[str, str],
) -> list[dict[str, str]]:
    prepared: list[dict[str, str]] = []
    seen_usernames: set[str] = set()
    for user in users:
        username = str(user.get("username") or "").strip()
        full_name = str(user.get("full_name") or "").strip()
        role = str(user.get("role") or "").strip().casefold()
        student_class = str(user.get("student_class") or "").strip()
        job_title = str(user.get("job_title") or "").strip()
        is_active = str(user.get("is_active") or "").strip().casefold()
        joined_date = str(user.get("joined_date") or "").strip()
        username_key = username.casefold()

        if role not in {"teacher", "student"}:
            raise CardExportError("final roster contains an unsupported role")
        if is_active not in {"t", "true", "1", "yes"}:
            raise CardExportError("final roster contains an inactive account")
        if len(username) < 2 or len(full_name) < 2:
            raise CardExportError("final roster contains incomplete identity data")
        if role == "student" and not student_class:
            raise CardExportError("final roster contains a student without a class")
        if username_key in seen_usernames:
            raise CardExportError("final roster contains a duplicate username")
        password = passwords.get(username_key)
        if not password:
            raise CardExportError(
                "a final account has no recoverable plaintext password"
            )
        seen_usernames.add(username_key)
        prepared.append(
            {
                "Role": "guru" if role == "teacher" else "siswa",
                "Nama Lengkap": full_name,
                "Username": username,
                "Kelas/Jabatan": (
                    (job_title or "Guru") if role == "teacher" else student_class
                ),
                "Password": password,
                "Status": "Aktif",
                "Bergabung": joined_date,
                "_role": role,
                "_class": student_class,
            }
        )

    prepared.sort(
        key=lambda row: (
            0 if row["_role"] == "teacher" else 1,
            class_sort_key(row["_class"]),
            row["Nama Lengkap"].casefold(),
        )
    )
    output: list[dict[str, str]] = []
    for number, row in enumerate(prepared, start=1):
        output.append(
            {
                "No": str(number),
                **{field: row[field] for field in OUTPUT_FIELDS if field != "No"},
            }
        )
    return output


def validate_counts(
    rows: Sequence[dict[str, str]],
    *,
    expected_teachers: int,
    expected_students: int,
) -> None:
    counts = Counter(row["Role"] for row in rows)
    # Compare per key: Counter omits absent keys, so an expectation of 0 would
    # never match a dict literal that still carries that key.
    if (
        counts["guru"] != expected_teachers
        or counts["siswa"] != expected_students
        or len(rows) != expected_teachers + expected_students
    ):
        raise CardExportError(
            "role counts do not match the expected production account totals"
        )


def write_private_csv(path: Path, rows: Sequence[dict[str, str]]) -> None:
    # Only tighten permissions on a directory this script created. Chmodding an
    # existing parent would silently narrow an unrelated directory.
    created_parent = not path.parent.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    if created_parent:
        path.parent.chmod(stat.S_IRWXU)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(OUTPUT_FIELDS))
        writer.writeheader()
        writer.writerows(rows)
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--users", type=Path, required=True)
    parser.add_argument("--legacy-credentials", type=Path, required=True)
    parser.add_argument("--changed-credentials", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expect-teachers", type=int, default=45)
    parser.add_argument("--expect-students", type=int, default=485)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        users = _read_csv(args.users.expanduser().resolve())
        passwords = load_legacy_passwords(
            args.legacy_credentials.expanduser().resolve()
        )
        passwords.update(
            load_changed_passwords(args.changed_credentials.expanduser().resolve())
        )
        rows = build_card_rows(users, passwords)
        validate_counts(
            rows,
            expected_teachers=args.expect_teachers,
            expected_students=args.expect_students,
        )
        output = args.output.expanduser().resolve()
        write_private_csv(output, rows)
        print(
            f"Card CSV created: {output} "
            f"(teachers={args.expect_teachers}, students={args.expect_students})"
        )
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI boundary emits no row data.
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
