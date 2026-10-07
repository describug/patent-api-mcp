import pytest

from core.errors import AMBIGUOUS_NUMBER, INVALID_INPUT, PatentApiError
from core.numbers import normalize_kr_application_number, normalize_ops_number


# ---------------------------------------------------------------------------
# 한국 출원번호 (kr_biblio)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "10-2026-0012345",
        "1020260012345",
        "KR10-2026-0012345",
        "KR 10-2026-0012345",
        "kr10-2026-0012345",
        " 10-2026-0012345 ",
        "10 2026 0012345",
        "10.2026.0012345",
        "10-2026-12345",  # 일련번호 0 채움
        "KR-1020260012345",
        "10‐2026‐0012345",  # 유니코드 하이픈
    ],
)
def test_kr_application_accepts(raw):
    assert normalize_kr_application_number(raw) == "1020260012345"


def test_kr_utility_model_application():
    assert normalize_kr_application_number("20-2025-0001234") == "2020250001234"


@pytest.mark.parametrize(
    "raw, fragment",
    [
        ("KR 10-2026-0012345 A", "종류코드"),
        ("10-2026-0012345A", "종류코드"),
        ("KR10-1234567B1", "종류코드"),
        ("10-1234567", "등록번호"),
        ("101234567", "등록번호"),
        ("30-2026-0012345", "디자인"),
        ("40-2026-0012345", "상표"),
        ("10-2026-001234567", "형식"),
        ("10-1820-0012345", "형식"),  # 연도 범위 밖
        ("abc", "형식"),
        ("", "비어"),
        ("EP1234567", "형식"),
    ],
)
def test_kr_application_rejects(raw, fragment):
    with pytest.raises(PatentApiError) as ei:
        normalize_kr_application_number(raw)
    assert ei.value.code == INVALID_INPUT
    assert fragment in ei.value.message


# ---------------------------------------------------------------------------
# OPS 번호
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, path, display",
    [
        ("EP1234567A1", "publication/docdb/EP.1234567.A1", "EP1234567A1"),
        ("EP 1234567 A1", "publication/docdb/EP.1234567.A1", "EP1234567A1"),
        ("EP 1 234 567 B1", "publication/docdb/EP.1234567.B1", "EP1234567B1"),
        ("ep1234567", "publication/epodoc/EP1234567", "EP1234567"),
        ("EP0123456A1", "publication/docdb/EP.0123456.A1", "EP0123456A1"),
        ("EP123456", "publication/epodoc/EP0123456", "EP0123456"),
        ("US 2026/0123456 A1", "publication/docdb/US.2026123456.A1", "US2026123456A1"),
        ("US20260123456A1", "publication/docdb/US.2026123456.A1", "US2026123456A1"),
        ("US2026123456A1", "publication/docdb/US.2026123456.A1", "US2026123456A1"),
        ("US 2022/0181889 A1", "publication/docdb/US.2022181889.A1", "US2022181889A1"),
        ("US 10,123,456 B2", "publication/docdb/US.10123456.B2", "US10123456B2"),
        ("US7654321", "publication/epodoc/US7654321", "US7654321"),
        ("WO2026/012345", "publication/epodoc/WO2026012345", "WO2026012345"),
        ("WO 2026/012345 A1", "publication/docdb/WO.2026012345.A1", "WO2026012345A1"),
        ("WO2026012345A1", "publication/docdb/WO.2026012345.A1", "WO2026012345A1"),
        ("WO 02/12345", "publication/epodoc/WO2002012345", "WO2002012345"),
        ("WO 99/1234", "publication/epodoc/WO1999001234", "WO1999001234"),
        ("KR 10-2026-0012345 A", "publication/docdb/KR.20260012345.A", "KR20260012345A"),
        ("KR10-2026-0012345A", "publication/docdb/KR.20260012345.A", "KR20260012345A"),
        ("KR 20-2025-0001234 U", "publication/docdb/KR.20250001234.U", "KR20250001234U"),
        ("KR 10-1234567 B1", "publication/docdb/KR.101234567.B1", "KR101234567B1"),
        ("KR10-1234567", "publication/docdb/KR.101234567.B1", "KR101234567B1"),
        ("10-1234567 B1", "publication/docdb/KR.101234567.B1", "KR101234567B1"),
        ("KR20260012345A", "publication/docdb/KR.20260012345.A", "KR20260012345A"),
        ("KR101234567B1", "publication/docdb/KR.101234567.B1", "KR101234567B1"),
        ("JP2026-123456A", "publication/docdb/JP.2026123456.A", "JP2026123456A"),
        ("JP 2026-012345 A", "publication/docdb/JP.2026012345.A", "JP2026012345A"),
        ("CN 112345678 A", "publication/docdb/CN.112345678.A", "CN112345678A"),
        ("DE102020123456A1", "publication/docdb/DE.102020123456.A1", "DE102020123456A1"),
    ],
)
def test_ops_publication(raw, path, display):
    ref = normalize_ops_number(raw)
    assert ref.path() == path
    assert ref.display == display
    assert ref.ref_type == "publication"


def test_ops_kr_application_explicit():
    ref = normalize_ops_number("10-2026-0012345", "application")
    assert ref.ref_type == "application"
    assert ref.path() == "application/docdb/KR.20260012345.A"


def test_ops_kr_publication_explicit_without_kind():
    ref = normalize_ops_number("KR 10-2026-0012345", "publication")
    assert ref.path() == "publication/epodoc/KR20260012345"


def test_ops_ep_application_explicit():
    ref = normalize_ops_number("EP 15700001.2", "application")
    assert ref.path() == "application/docdb/EP.15700001.A"


@pytest.mark.parametrize(
    "raw",
    [
        "KR 10-2026-0012345",  # 출원? 공개? 모양이 같다
        "10-2026-0012345",
        "1020260012345",
        "KR20260012345",
        "EP 15700001.2",  # EP 출원번호 모양
        "EP15700001",
    ],
)
def test_ops_ambiguous(raw):
    with pytest.raises(PatentApiError) as ei:
        normalize_ops_number(raw)
    assert ei.value.code == AMBIGUOUS_NUMBER


@pytest.mark.parametrize(
    "raw, args, fragment",
    [
        ("1234567", (), "국가코드"),
        ("PCT/KR2026/001234", (), "PCT"),
        ("KR 10-2026-0012345 B1", (), "종류코드"),
        ("KR 10-1234567 A", (), "종류코드"),
        ("EP1234567A1", ("application",), "종류코드"),
        ("KR 10-1234567", ("application",), "등록번호"),
        ("KR 30-2026-0012345", (), "디자인"),
        ("EP12345678901", ("publication",), "7자리"),
        ("hello", (), "해석할 수 없습니다"),
        ("", (), "비어"),
        ("EP1234567", ("granted",), "number_type"),
        ("WO2026/0123456789", (), "WO"),
    ],
)
def test_ops_invalid(raw, args, fragment):
    with pytest.raises(PatentApiError) as ei:
        normalize_ops_number(raw, *args)
    assert ei.value.code == INVALID_INPUT
    assert fragment in ei.value.message


def test_ops_kr_utility_application_uses_u():
    ref = normalize_ops_number("20-2025-0001234", "application")
    assert ref.path() == "application/docdb/KR.20250001234.U"


def test_ops_kr_registration_without_kind_assumes_b1():
    ref = normalize_ops_number("KR 10-1500000")
    assert ref.kind == "B1" and ref.kind_assumed
    assert normalize_ops_number("KR 20-0412345").kind == "Y1"
    assert not normalize_ops_number("KR 10-1500000 B1").kind_assumed
