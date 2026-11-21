#!/usr/bin/env python3
"""Reconcile school users from a legacy credential CSV and current class roster.

Dry-run is the default. Reports containing personal data are always private and
passwords are never written until a successful apply operation.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import re
import secrets
import stat
import string
import sys
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

CURRENT_SHEETS = {
    "XA": "X A",
    "XB": "X B",
    "XC": "X C",
    "XD": "X D",
    "XE": "X E",
    "XF": "X F",
    "XIA": "XI A",
    "XIB": "XI B",
    "XIC": "XI C",
    "XID": "XI D",
    "XIE": "XI E",
    "XIIA": "XII A",
    "XIIB": "XII B",
    "XIIC": "XII C",
    "XIID": "XII D",
    "XIIE": "XII E",
}
REVIEW_FIELDS = (
    "decision",
    "source_username",
    "source_name",
    "old_class",
    "target_id",
    "target_name",
    "target_class",
    "similarity",
    "username_token_match",
    "approved",
)
CREDENTIAL_FIELDS = (
    "username",
    "password",
    "full_name",
    "role",
    "student_class",
    "reason",
)


class SyncError(RuntimeError):
    """Raised when reconciliation cannot proceed safely."""


@dataclass(frozen=True)
class SourceUser:
    row_number: int
    username: str
    password: str
    full_name: str
    role: str
    old_class: str | None = None
    job_title: str | None = None

    @property
    def normalized_name(self) -> str:
        return normalize_person_name(self.full_name)


@dataclass(frozen=True)
class RosterStudent:
    target_id: str
    full_name: str
    student_class: str
    sheet_name: str
    row_number: int
    nisn: str | None = None

    @property
    def normalized_name(self) -> str:
        return normalize_person_name(self.full_name)


@dataclass
class ReconciliationPlan:
    teachers: list[SourceUser] = field(default_factory=list)
    exact_matches: list[tuple[SourceUser, RosterStudent]] = field(default_factory=list)
    review_sources: list[SourceUser] = field(default_factory=list)
    review_targets: list[RosterStudent] = field(default_factory=list)
    new_students: list[RosterStudent] = field(default_factory=list)
    graduates: list[SourceUser] = field(default_factory=list)


@dataclass
class ReviewResolution:
    matches: list[tuple[SourceUser, RosterStudent]] = field(default_factory=list)
    excluded_sources: list[SourceUser] = field(default_factory=list)
    new_students: list[RosterStudent] = field(default_factory=list)


@dataclass
class DesiredUser:
    username: str
    password: str
    full_name: str
    role: str
    student_class: str | None
    job_title: str | None
    credential_reason: str | None
    is_new_account: bool = False


def normalize_person_name(value: Any) -> str:
    import unicodedata

    text = str(value or "").strip().upper()
    text = "".join(
        char
        for char in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(char)
    )
    text = text.replace("’", "'").replace("`", "'")
    text = re.sub(r"[^A-Z0-9' ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_class_name(value: Any) -> str | None:
    compact = re.sub(r"[\s\-]+", "", str(value or "").strip().upper())
    match = re.fullmatch(r"(XII|XI|X)([A-F])", compact)
    if not match:
        return None
    return f"{match.group(1)} {match.group(2)}"


def class_grade(value: Any) -> str | None:
    normalized = normalize_class_name(value)
    return normalized.split()[0] if normalized else None


def password_meets_policy(password: str) -> bool:
    return (
        len(password) >= 8
        and bool(re.search(r"[A-Z]", password))
        and bool(re.search(r"[a-z]", password))
        and bool(re.search(r"[0-9]", password))
    )


def generate_password(length: int = 12) -> str:
    if length < 8:
        raise ValueError("password length must be at least 8")
    alphabet = string.ascii_letters + string.digits
    characters = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
    ]
    characters.extend(secrets.choice(alphabet) for _ in range(length - len(characters)))
    secrets.SystemRandom().shuffle(characters)
    return "".join(characters)


def secure_path(path: Path) -> Path:
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return path


def ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(stat.S_IRWXU)
    return path


def _normalized_headers(headers: Sequence[str]) -> dict[str, str]:
    return {
        re.sub(r"\s+", " ", str(header or "").strip()).casefold(): header
        for header in headers
    }


def load_source_users(csv_path: Path) -> tuple[list[SourceUser], list[SourceUser]]:
    if not csv_path.is_file():
        raise SyncError(f"CSV source not found: {csv_path}")
    secure_path(csv_path)
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise SyncError("CSV source has no header")
        headers = _normalized_headers(reader.fieldnames)
        required = {
            "role": "Role",
            "nama lengkap": "Nama Lengkap",
            "username": "Username",
            "kelas/jabatan": "Kelas/Jabatan",
            "password": "Password",
        }
        missing = [display for key, display in required.items() if key not in headers]
        if missing:
            raise SyncError(f"CSV source missing columns: {', '.join(missing)}")

        teachers: list[SourceUser] = []
        students: list[SourceUser] = []
        seen_usernames: set[str] = set()
        for row_number, row in enumerate(reader, start=2):
            role_value = str(row.get(headers["role"]) or "").strip().casefold()
            role = {"guru": "teacher", "siswa": "student"}.get(role_value)
            if role is None:
                raise SyncError(f"unsupported role at CSV row {row_number}")
            username = str(row.get(headers["username"]) or "").strip()
            password = str(row.get(headers["password"]) or "").strip()
            full_name = str(row.get(headers["nama lengkap"]) or "").strip()
            class_or_title = str(row.get(headers["kelas/jabatan"]) or "").strip()
            if not username or not password or not full_name:
                raise SyncError(f"incomplete required data at CSV row {row_number}")
            username_key = username.casefold()
            if username_key in seen_usernames:
                raise SyncError(f"duplicate username at CSV row {row_number}")
            seen_usernames.add(username_key)

            if role == "teacher":
                job_title = (
                    class_or_title if class_or_title and class_or_title != "-" else None
                )
                teachers.append(
                    SourceUser(
                        row_number=row_number,
                        username=username,
                        password=password,
                        full_name=full_name,
                        role=role,
                        job_title=job_title,
                    )
                )
                continue

            student_class = normalize_class_name(class_or_title)
            if student_class is None:
                raise SyncError(f"invalid student class at CSV row {row_number}")
            students.append(
                SourceUser(
                    row_number=row_number,
                    username=username,
                    password=password,
                    full_name=full_name,
                    role=role,
                    old_class=student_class,
                )
            )
    return teachers, students


def load_current_roster(excel_path: Path) -> list[RosterStudent]:
    if not excel_path.is_file():
        raise SyncError(f"Excel roster not found: {excel_path}")
    secure_path(excel_path)
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise SyncError("openpyxl is required to read the roster") from exc

    workbook = load_workbook(excel_path, read_only=True, data_only=True)
    roster: list[RosterStudent] = []
    try:
        for sheet_name in workbook.sheetnames:
            compact_sheet = re.sub(r"\s+", "", sheet_name.strip().upper())
            student_class = CURRENT_SHEETS.get(compact_sheet)
            if student_class is None:
                continue
            worksheet = workbook[sheet_name]
            rows = list(worksheet.iter_rows(values_only=True))
            header_index: int | None = None
            headers: list[str] = []
            for index, row in enumerate(rows[:20]):
                values = [
                    str(cell).strip().upper() if cell is not None else ""
                    for cell in row
                ]
                if "NAMA SISWA" in values or "NAMA" in values:
                    header_index = index
                    headers = values
                    break
            if header_index is None:
                raise SyncError(
                    f"student header not found in current sheet {sheet_name!r}"
                )
            name_column = (
                headers.index("NAMA SISWA")
                if "NAMA SISWA" in headers
                else headers.index("NAMA")
            )
            nisn_column = headers.index("NISN") if "NISN" in headers else None
            for row_index, row in enumerate(
                rows[header_index + 1 :], start=header_index + 2
            ):
                full_name = (
                    str(row[name_column] or "").strip()
                    if name_column < len(row)
                    else ""
                )
                if not full_name:
                    continue
                nisn = None
                if nisn_column is not None and nisn_column < len(row):
                    raw_nisn = str(row[nisn_column] or "").strip().removesuffix(".0")
                    if re.fullmatch(r"\d{10}", raw_nisn):
                        nisn = raw_nisn
                roster.append(
                    RosterStudent(
                        target_id=f"{compact_sheet}:{row_index}",
                        full_name=full_name,
                        student_class=student_class,
                        sheet_name=sheet_name,
                        row_number=row_index,
                        nisn=nisn,
                    )
                )
    finally:
        workbook.close()

    if not roster:
        raise SyncError("current roster contains no readable students")
    names = Counter(item.normalized_name for item in roster)
    if any(count > 1 for count in names.values()):
        raise SyncError("current roster contains duplicate normalized student names")
    return roster


def expected_previous_grade(target_class: str) -> str | None:
    return {"XI": "X", "XII": "XI"}.get(class_grade(target_class))


def build_reconciliation_plan(
    teachers: Sequence[SourceUser],
    students: Sequence[SourceUser],
    roster: Sequence[RosterStudent],
) -> ReconciliationPlan:
    plan = ReconciliationPlan(teachers=list(teachers))
    continuing = [
        item for item in students if class_grade(item.old_class) in {"X", "XI"}
    ]
    plan.graduates = [item for item in students if class_grade(item.old_class) == "XII"]
    sources_by_name: dict[str, list[SourceUser]] = defaultdict(list)
    for source in continuing:
        sources_by_name[source.normalized_name].append(source)

    used_usernames: set[str] = set()
    for target in roster:
        if class_grade(target.student_class) == "X":
            plan.new_students.append(target)
            continue
        previous_grade = expected_previous_grade(target.student_class)
        candidates = [
            source
            for source in sources_by_name.get(target.normalized_name, [])
            if class_grade(source.old_class) == previous_grade
            and source.username.casefold() not in used_usernames
        ]
        if len(candidates) == 1:
            source = candidates[0]
            used_usernames.add(source.username.casefold())
            plan.exact_matches.append((source, target))

    plan.review_sources = [
        source
        for source in continuing
        if source.username.casefold() not in used_usernames
    ]
    matched_targets = {target.target_id for _, target in plan.exact_matches}
    plan.review_targets = [
        target
        for target in roster
        if class_grade(target.student_class) in {"XI", "XII"}
        and target.target_id not in matched_targets
    ]
    return plan


def _username_name_token_match(source: SourceUser, target: RosterStudent) -> bool:
    username_tokens = [
        re.sub(r"[^A-Z0-9]", "", token.upper()) for token in source.username.split(".")
    ]
    target_tokens = set(target.normalized_name.split())
    return any(token and token in target_tokens for token in username_tokens)


def build_review_rows(plan: ReconciliationPlan) -> list[dict[str, str]]:
    edges: list[tuple[float, bool, SourceUser, RosterStudent]] = []
    for source in plan.review_sources:
        expected_grade = "XI" if class_grade(source.old_class) == "X" else "XII"
        for target in plan.review_targets:
            if class_grade(target.student_class) != expected_grade:
                continue
            similarity = SequenceMatcher(
                None,
                source.normalized_name,
                target.normalized_name,
            ).ratio()
            token_match = _username_name_token_match(source, target)
            edges.append((similarity, token_match, source, target))

    edges.sort(key=lambda item: (item[0], item[1]), reverse=True)
    used_sources: set[str] = set()
    used_targets: set[str] = set()
    rows: list[dict[str, str]] = []
    for similarity, token_match, source, target in edges:
        source_key = source.username.casefold()
        if source_key in used_sources or target.target_id in used_targets:
            continue
        used_sources.add(source_key)
        used_targets.add(target.target_id)
        rows.append(
            {
                "decision": "match",
                "source_username": source.username,
                "source_name": source.full_name,
                "old_class": source.old_class or "",
                "target_id": target.target_id,
                "target_name": target.full_name,
                "target_class": target.student_class,
                "similarity": f"{similarity:.4f}",
                "username_token_match": "yes" if token_match else "no",
                "approved": "",
            }
        )

    for source in plan.review_sources:
        if source.username.casefold() in used_sources:
            continue
        rows.append(
            {
                "decision": "exclude",
                "source_username": source.username,
                "source_name": source.full_name,
                "old_class": source.old_class or "",
                "target_id": "",
                "target_name": "",
                "target_class": "",
                "similarity": "",
                "username_token_match": "",
                "approved": "",
            }
        )

    for target in plan.review_targets:
        if target.target_id in used_targets:
            continue
        rows.append(
            {
                "decision": "new",
                "source_username": "",
                "source_name": "",
                "old_class": "",
                "target_id": target.target_id,
                "target_name": target.full_name,
                "target_class": target.student_class,
                "similarity": "",
                "username_token_match": "",
                "approved": "",
            }
        )
    return sorted(
        rows,
        key=lambda row: (
            row["decision"],
            row["old_class"],
            row["source_username"].casefold(),
            row["target_id"],
        ),
    )


def write_private_csv(
    path: Path, rows: Sequence[dict[str, Any]], fields: Sequence[str]
) -> Path:
    ensure_private_dir(path.parent)
    # utf-8-sig so the review CSV opens with correct non-ASCII names in Excel.
    # Every reader here already accepts utf-8-sig, so the BOM round-trips.
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields))
        writer.writeheader()
        writer.writerows(
            {field: row.get(field, "") for field in fields} for row in rows
        )
    return secure_path(path)


def load_approved_mapping(
    mapping_path: Path,
    plan: ReconciliationPlan,
) -> ReviewResolution:
    if not mapping_path.is_file():
        raise SyncError(f"approved mapping not found: {mapping_path}")
    secure_path(mapping_path)
    sources = {item.username.casefold(): item for item in plan.review_sources}
    targets = {item.target_id: item for item in plan.review_targets}
    resolution = ReviewResolution()
    used_sources: set[str] = set()
    used_targets: set[str] = set()
    with mapping_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"decision", "source_username", "target_id", "approved"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise SyncError("approved mapping has invalid columns")
        for row in reader:
            if str(row.get("approved") or "").strip().casefold() not in {
                "yes",
                "ya",
                "y",
                "1",
                "true",
            }:
                continue
            decision = str(row.get("decision") or "").strip().casefold()
            source_key = str(row.get("source_username") or "").strip().casefold()
            target_id = str(row.get("target_id") or "").strip()
            if decision == "match":
                source = sources.get(source_key)
                target = targets.get(target_id)
                if source is None or target is None:
                    raise SyncError(
                        "approved match references an unknown source or target"
                    )
                if source_key in used_sources or target_id in used_targets:
                    raise SyncError(
                        "approved mapping assigns a source or target more than once"
                    )
                if expected_previous_grade(target.student_class) != class_grade(
                    source.old_class
                ):
                    raise SyncError(
                        "approved mapping crosses an invalid grade transition"
                    )
                used_sources.add(source_key)
                used_targets.add(target_id)
                resolution.matches.append((source, target))
            elif decision == "exclude":
                source = sources.get(source_key)
                if source is None or target_id:
                    raise SyncError("approved exclude row is invalid")
                if source_key in used_sources:
                    raise SyncError(
                        "approved mapping assigns one source more than once"
                    )
                used_sources.add(source_key)
                resolution.excluded_sources.append(source)
            elif decision == "new":
                target = targets.get(target_id)
                if target is None or source_key:
                    raise SyncError("approved new row is invalid")
                if target_id in used_targets:
                    raise SyncError(
                        "approved mapping assigns one target more than once"
                    )
                used_targets.add(target_id)
                resolution.new_students.append(target)
            else:
                raise SyncError("approved mapping contains an invalid decision")
    if len(used_sources) != len(plan.review_sources) or len(used_targets) != len(
        plan.review_targets
    ):
        raise SyncError(
            "approved mapping is incomplete: every review source and target must be resolved"
        )
    return resolution


def _username_stem(full_name: str) -> str:
    normalized = normalize_person_name(full_name).lower()
    token = normalized.split()[0] if normalized else "siswa"
    stem = re.sub(r"[^a-z0-9]", "", token)
    return (stem or "siswa")[:15]


def generate_school_username(
    full_name: str,
    identity_key: str,
    used_usernames: set[str],
) -> str:
    stem = _username_stem(full_name)
    digest = hashlib.sha256(
        f"{identity_key}|{normalize_person_name(full_name)}".encode()
    ).digest()
    initial_suffix = int.from_bytes(digest[:4], "big") % 9000
    for offset in range(9000):
        candidate = f"{stem}.{1000 + ((initial_suffix + offset) % 9000)}"
        key = candidate.casefold()
        if key not in used_usernames:
            used_usernames.add(key)
            return candidate
    raise SyncError(f"unable to generate a unique username for stem {stem!r}")


def build_desired_users(
    plan: ReconciliationPlan,
    review_resolution: ReviewResolution,
) -> list[DesiredUser]:
    desired: list[DesiredUser] = []
    used_usernames = {teacher.username.casefold() for teacher in plan.teachers}
    matches = [*plan.exact_matches, *review_resolution.matches]
    used_usernames.update(source.username.casefold() for source, _ in matches)
    # Legacy accounts that stay in the database but are not part of the desired
    # set must still reserve their username. Generating one of them for a new
    # student would overwrite that account instead of creating a fresh one.
    used_usernames.update(source.username.casefold() for source in plan.graduates)
    used_usernames.update(
        source.username.casefold() for source in review_resolution.excluded_sources
    )

    for teacher in plan.teachers:
        password = teacher.password
        reason = None
        if not password_meets_policy(password):
            password = generate_password()
            reason = "hardened-legacy-password"
        desired.append(
            DesiredUser(
                username=teacher.username,
                password=password,
                full_name=teacher.full_name,
                role="teacher",
                student_class=None,
                job_title=teacher.job_title,
                credential_reason=reason,
            )
        )

    for source, target in matches:
        password = source.password
        reason = None
        if not password_meets_policy(password):
            password = generate_password()
            reason = "hardened-legacy-password"
        desired.append(
            DesiredUser(
                username=source.username,
                password=password,
                full_name=target.full_name,
                role="student",
                student_class=target.student_class,
                job_title=None,
                credential_reason=reason,
            )
        )

    for target in [*plan.new_students, *review_resolution.new_students]:
        desired.append(
            DesiredUser(
                username=generate_school_username(
                    target.full_name,
                    target.target_id,
                    used_usernames,
                ),
                password=generate_password(),
                full_name=target.full_name,
                role="student",
                student_class=target.student_class,
                job_title=None,
                credential_reason="new-student",
                is_new_account=True,
            )
        )

    usernames = Counter(item.username.casefold() for item in desired)
    if any(count > 1 for count in usernames.values()):
        raise SyncError("desired account set contains duplicate usernames")
    return desired


def changed_credentials(desired: Sequence[DesiredUser]) -> list[dict[str, Any]]:
    return [
        {
            "username": item.username,
            "password": item.password,
            "full_name": item.full_name,
            "role": item.role,
            "student_class": item.student_class or "",
            "reason": item.credential_reason,
        }
        for item in desired
        if item.credential_reason
    ]


def build_summary(plan: ReconciliationPlan) -> dict[str, Any]:
    weak_teacher_passwords = sum(
        not password_meets_policy(item.password) for item in plan.teachers
    )
    continuing_sources = [*plan.exact_matches]
    weak_student_passwords = sum(
        not password_meets_policy(source.password) for source, _ in continuing_sources
    ) + sum(
        not password_meets_policy(source.password) for source in plan.review_sources
    )
    class_counts = Counter(target.student_class for _, target in plan.exact_matches)
    class_counts.update(item.student_class for item in plan.review_targets)
    class_counts.update(item.student_class for item in plan.new_students)
    return {
        "teachers": len(plan.teachers),
        "students": {
            "exact_matches": len(plan.exact_matches),
            "needs_review": len(plan.review_sources),
            "new_class_x": len(plan.new_students),
            "graduates_excluded": len(plan.graduates),
            "final_total": len(plan.exact_matches)
            + len(plan.review_targets)
            + len(plan.new_students),
            "classes": dict(sorted(class_counts.items())),
        },
        "passwords": {
            "legacy_teacher_to_harden": weak_teacher_passwords,
            "legacy_student_to_harden": weak_student_passwords,
            "new_student_passwords": len(plan.new_students),
        },
        "expected_database": {
            "developer": 1,
            "admin": 1,
            "teacher": len(plan.teachers),
            "student": len(plan.exact_matches)
            + len(plan.review_targets)
            + len(plan.new_students),
            "total": 2
            + len(plan.teachers)
            + len(plan.exact_matches)
            + len(plan.review_targets)
            + len(plan.new_students),
        },
    }


async def apply_users(desired: Sequence[DesiredUser]) -> None:
    from sqlalchemy import func, select

    from app.core.security import get_password_hash, verify_password
    from app.core.users_cache import (
        _invalidate_user_caches,
        _invalidate_user_caches_redis,
    )
    from app.database import async_session_write
    from app.models.exam import Exam
    from app.models.session import ExamSession
    from app.models.user import User

    password_hashes = {
        item.username.casefold(): get_password_hash(item.password) for item in desired
    }
    for item in desired:
        if not verify_password(
            item.password, password_hashes[item.username.casefold()]
        ):
            raise SyncError("generated password hash verification failed")
    async with async_session_write() as db:
        active_exams = await db.scalar(
            select(func.count(Exam.id)).where(
                Exam.is_published.is_(True),
                Exam.start_time <= func.now(),
                Exam.end_time >= func.now(),
            )
        )
        active_sessions = await db.scalar(
            select(func.count(ExamSession.id)).where(
                ExamSession.status.in_(("in_progress", "active"))
            )
        )
        if int(active_exams or 0) or int(active_sessions or 0):
            raise SyncError(
                "production safety gate failed: active exam or session exists"
            )

        existing_result = await db.execute(select(User))
        existing = {
            user.username.casefold(): user for user in existing_result.scalars().all()
        }
        for item in desired:
            key = item.username.casefold()
            user = existing.get(key)
            if user is not None and str(user.role or "").strip().lower() != item.role:
                raise SyncError(
                    "desired username conflicts with a different existing role"
                )
            if user is not None and item.is_new_account:
                raise SyncError(
                    "generated username for a new student already exists; "
                    "refusing to overwrite an existing account"
                )
            if user is None:
                user = User(
                    username=item.username, created_at=datetime.now(timezone.utc)
                )
                db.add(user)
                existing[key] = user
            user.password_hash = password_hashes[key]
            user.full_name = item.full_name
            user.role = item.role
            user.student_class = item.student_class
            user.job_title = item.job_title
            user.is_active = True
        await db.commit()

    async with async_session_write() as db:
        result = await db.execute(
            select(User).where(User.username.in_([item.username for item in desired]))
        )
        persisted = {user.username.casefold(): user for user in result.scalars().all()}
        if len(persisted) != len(desired):
            raise SyncError("post-commit verification found missing accounts")
        for item in desired:
            user = persisted[item.username.casefold()]
            if (
                str(user.role or "").strip().lower() != item.role
                or not user.is_active
                or user.student_class != item.student_class
                or user.password_hash != password_hashes[item.username.casefold()]
            ):
                raise SyncError("post-commit credential verification failed")

    _invalidate_user_caches()
    await _invalidate_user_caches_redis()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, required=True, help="Legacy credential CSV")
    parser.add_argument(
        "--excel", type=Path, required=True, help="Current class roster workbook"
    )
    parser.add_argument(
        "--report-dir", type=Path, required=True, help="Private report directory"
    )
    parser.add_argument("--mapping-file", type=Path, help="Reviewed reconciliation CSV")
    parser.add_argument(
        "--apply", action="store_true", help="Write reconciled accounts"
    )
    parser.add_argument("--allow-production-write", action="store_true")
    parser.add_argument("--i-understand-account-write", action="store_true")
    return parser.parse_args(argv)


async def async_main(args: argparse.Namespace) -> int:
    if args.apply and not (
        args.allow_production_write and args.i_understand_account_write
    ):
        raise SyncError(
            "apply requires --allow-production-write and --i-understand-account-write"
        )
    csv_path = args.csv.expanduser().resolve()
    excel_path = args.excel.expanduser().resolve()
    report_dir = ensure_private_dir(args.report_dir.expanduser().resolve())
    teachers, students = load_source_users(csv_path)
    roster = load_current_roster(excel_path)
    plan = build_reconciliation_plan(teachers, students, roster)
    summary = build_summary(plan)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    report_path = (
        report_dir
        / f"school_user_sync_{'apply' if args.apply else 'dry_run'}_{stamp}.json"
    )
    report_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    secure_path(report_path)

    review_path = None
    review_rows = build_review_rows(plan)
    if review_rows:
        review_path = report_dir / f"school_user_review_{stamp}.csv"
        write_private_csv(review_path, review_rows, REVIEW_FIELDS)

    if not args.apply:
        print(
            json.dumps(
                {
                    "mode": "dry-run",
                    "report": str(report_path),
                    "review": str(review_path) if review_path else None,
                    "summary": summary,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    # review_targets can be non-empty while review_sources is empty (transfer-in
    # students in XI/XII with no legacy counterpart). Both sides must go through
    # the reviewed mapping or those students never reach build_desired_users.
    needs_review = bool(plan.review_sources or plan.review_targets)
    if needs_review and args.mapping_file is None:
        raise SyncError(
            "apply is blocked until a complete reviewed mapping is supplied"
        )
    review_resolution = (
        load_approved_mapping(args.mapping_file.expanduser().resolve(), plan)
        if needs_review
        else ReviewResolution()
    )
    desired = build_desired_users(plan, review_resolution)
    credentials = changed_credentials(desired)
    pending_credentials_path = (
        report_dir / f"school_user_credentials_{stamp}.pending.csv"
    )
    write_private_csv(pending_credentials_path, credentials, CREDENTIAL_FIELDS)
    try:
        await apply_users(desired)
    except BaseException:
        # Honour the module contract: no plaintext passwords survive on disk for
        # accounts that were never written.
        pending_credentials_path.unlink(missing_ok=True)
        raise
    credentials_path = report_dir / f"school_user_credentials_{stamp}.csv"
    pending_credentials_path.replace(credentials_path)
    secure_path(credentials_path)
    print(
        json.dumps(
            {
                "mode": "apply",
                "report": str(report_path),
                "credentials": str(credentials_path),
                "accounts_applied": len(desired),
                "credentials_changed_or_created": len(credentials),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return asyncio.run(async_main(parse_args(argv)))
    except Exception as exc:  # noqa: BLE001 - CLI boundary returns a user-safe error.
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
