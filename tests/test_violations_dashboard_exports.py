from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from app.api.monitoring import (
    _build_violations_dashboard_payload,
)
from app.core.violations_dashboard import _coerce_violations_date_range
from app.core.pdf_generator import (
    REPORTLAB_AVAILABLE,
    ViolationsReportPDF,
    generate_violations_report_pdf,
)


def _make_log(
    *,
    log_id: int,
    created_at: datetime,
    event_type: str,
    event_data: dict,
    user_id: int,
    name: str,
    username: str,
    student_class: str,
    exam_id: int,
    exam_title: str,
    session_id: int,
):
    user = SimpleNamespace(
        id=user_id,
        full_name=name,
        username=username,
        student_class=student_class,
    )
    exam = SimpleNamespace(title=exam_title)
    session = SimpleNamespace(
        id=session_id,
        exam_id=exam_id,
        user=user,
        exam=exam,
        archived_exam_title=None,
    )
    return SimpleNamespace(
        id=log_id,
        session_id=session_id,
        event_type=event_type,
        event_data=event_data,
        created_at=created_at,
        session=session,
    )


def _build_payload():
    now = datetime(2026, 3, 6, 8, 0, tzinfo=timezone.utc)
    logs = [
        _make_log(
            log_id=1,
            created_at=now,
            event_type="violation_copy",
            event_data={"details": "Menekan Ctrl+C pada soal nomor 4", "action": "keyboard_ctrl_c", "source": "web"},
            user_id=10,
            name="Budi Santoso",
            username="budi",
            student_class="XII IPA 1",
            exam_id=101,
            exam_title="Try Out Matematika",
            session_id=1001,
        ),
        _make_log(
            log_id=2,
            created_at=now - timedelta(minutes=7),
            event_type="violation_copy",
            event_data={"details": "Percobaan copy ulang", "action": "keyboard_ctrl_c", "source": "web"},
            user_id=10,
            name="Budi Santoso",
            username="budi",
            student_class="XII IPA 1",
            exam_id=101,
            exam_title="Try Out Matematika",
            session_id=1001,
        ),
        _make_log(
            log_id=3,
            created_at=now - timedelta(minutes=15),
            event_type="violation_overlay_app",
            event_data={
                "details": "Aplikasi overlay terdeteksi",
                "overlay_apps": ["WhatsApp Bubble", "Clipboard Pro"],
                "source": "flutter_app",
            },
            user_id=11,
            name="Siti Rahma",
            username="siti",
            student_class="XII IPA 2",
            exam_id=102,
            exam_title="Simulasi Biologi",
            session_id=1002,
        ),
    ]
    return _build_violations_dashboard_payload(
        logs,
        exam_id=None,
        date_from=now - timedelta(days=1),
        date_to=now,
        selected_exam_title=None,
    )


def test_build_violations_dashboard_payload_includes_detailed_breakdowns():
    payload = _build_payload()
    type_index = {item["violation_type"]: item for item in payload["type_breakdown"]}

    assert payload["total_violations"] == 3
    assert payload["unique_offender_count"] == 2
    assert payload["top_offenders"][0]["username"] == "budi"
    assert payload["top_offenders"][0]["count"] == 2
    assert payload["top_offenders"][0]["type_breakdown"][0]["violation_type"] == "copy"
    assert type_index["overlay_app"]["offenders"][0]["username"] == "siti"
    assert type_index["copy"]["count"] == 2
    assert type_index["copy"]["explanation"]["simple"].startswith("Peserta mencoba menyalin")
    assert "kertas kecil" in type_index["copy"]["explanation"]["analogy"]
    assert payload["violations"][0]["detail_summary"] == "Menekan Ctrl+C pada soal nomor 4 | Aksi: keyboard ctrl c | Sumber: web"

def test_generate_violations_report_pdf_smoke():
    if not REPORTLAB_AVAILABLE:
        return

    payload = _build_payload()
    pdf_bytes = generate_violations_report_pdf(payload)

    assert pdf_bytes.startswith(b"%PDF")
    assert len(pdf_bytes) > 3000


def test_violations_pdf_management_summary_mentions_key_findings():
    if not REPORTLAB_AVAILABLE:
        return

    payload = _build_payload()
    generator = ViolationsReportPDF()
    summary = generator._build_management_summary(payload, generator._resolve_risk(payload))
    top_label = payload["type_breakdown"][0]["label"]

    assert "Budi Santoso" in summary
    assert top_label in summary
    assert "3" in summary


def test_coerce_violations_date_range_naive_treated_as_wib_and_normalized_utc():
    date_from = datetime(2026, 3, 12, 0, 0, 0)  # Naive from HTML date input (WIB day start)
    date_to = datetime(2026, 3, 12, 23, 59, 59)  # Naive from HTML date input (WIB day end)

    effective_from, effective_to = _coerce_violations_date_range(date_from, date_to)

    assert effective_from.tzinfo == timezone.utc
    assert effective_to.tzinfo == timezone.utc
    assert effective_from.isoformat() == "2026-03-11T17:00:00+00:00"
    assert effective_to.isoformat() == "2026-03-12T16:59:59+00:00"


def test_every_pdf_class_resolves_logo_from_both_candidates(tmp_path, monkeypatch):
    """Every logo-bearing PDF class must accept either logo location.

    Production bind-mounts static/ but not tools/, so a tools-only lookup
    silently drops the logo from that report. CertificatePDF is excluded on
    purpose: it draws no logo.
    """
    if not REPORTLAB_AVAILABLE:
        return

    from app.core import pdf_generator

    classes = [
        pdf_generator.ExamResultPDF,
        pdf_generator.ViolationsReportPDF,
        pdf_generator.ExamAnalyticsPDF,
    ]
    relative_candidates = [
        Path("static") / "uploads" / "logo-man1.jpeg",
        Path("tools") / "logoman1.jpeg",
    ]

    for relative in relative_candidates:
        base_dir = tmp_path / relative.parts[0] / "root"
        fake_module = base_dir / "app" / "core" / "pdf_generator.py"
        logo = base_dir / relative
        logo.parent.mkdir(parents=True, exist_ok=True)
        logo.write_bytes(b"not-a-real-jpeg")
        monkeypatch.setattr(pdf_generator, "__file__", str(fake_module))

        for cls in classes:
            resolved = cls._resolve_logo_path(cls.__new__(cls))
            assert resolved == str(logo), f"{cls.__name__} missed {relative}"


def test_violations_pdf_embeds_the_tracked_logo_image(tmp_path, monkeypatch):
    """The resolved logo must reach the rendered PDF as an embedded image.

    Resolving a path is not enough; the cover page has to draw it. This
    renders against tools/logoman1.jpeg because static/uploads/ is
    gitignored, so tools/ is the only candidate a fresh checkout ships.
    """
    if not REPORTLAB_AVAILABLE:
        return

    from app.core import pdf_generator

    repo_root = Path(pdf_generator.__file__).resolve().parents[2]
    tracked_logo = repo_root / "tools" / "logoman1.jpeg"
    assert tracked_logo.exists(), "tools/logoman1.jpeg must stay tracked for fresh checkouts"

    base_dir = tmp_path / "root"
    staged_logo = base_dir / "tools" / "logoman1.jpeg"
    staged_logo.parent.mkdir(parents=True, exist_ok=True)
    staged_logo.write_bytes(tracked_logo.read_bytes())
    monkeypatch.setattr(
        pdf_generator, "__file__", str(base_dir / "app" / "core" / "pdf_generator.py")
    )

    generator = pdf_generator.ViolationsReportPDF()
    assert generator.logo_path == str(staged_logo)

    pdf_bytes = generator.generate(_build_payload())

    assert pdf_bytes.startswith(b"%PDF")
    assert b"/Image" in pdf_bytes, "cover page rendered without an image XObject"
    assert b"DCTDecode" in pdf_bytes, "logo JPEG was not embedded in the PDF"
