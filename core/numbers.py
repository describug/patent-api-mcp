"""번호 정규화.

사용자가 적는 번호는 제각각이다. 입력은 관대하게 받고, 내부에서 KIPRIS·OPS 형식으로 맞춘다.
번호 종류(출원/공개/등록)를 특정할 수 없으면 추측하지 않고 AMBIGUOUS_NUMBER 오류를 낸다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from .errors import AMBIGUOUS_NUMBER, INVALID_INPUT, PatentApiError

RefType = Literal["publication", "application"]

# 한국 출원번호 앞 두 자리(권리 종류). 특허·실용신안만 다룬다.
_KR_RIGHT_CODES = {"10": "특허", "20": "실용신안"}
_KR_OTHER_RIGHT_CODES = {"30": "디자인", "40": "상표", "41": "상표", "45": "상표"}

# 종류코드: 영문 1자 + 숫자 0~1자 (A, A1, B1, U, Y1 ...)
_KIND_RE = r"[A-Z][0-9]?"

# 번호 사이에 흔히 끼는 구분자
_SEPARATORS = re.compile(r"[\s\-./,]")


def _clean(raw: str) -> str:
    if not isinstance(raw, str):
        raise PatentApiError(INVALID_INPUT, "번호는 문자열로 입력해야 합니다.")
    s = raw.strip().upper()
    s = s.replace("‐", "-").replace("‑", "-").replace("–", "-").replace("—", "-")
    s = s.replace(" ", " ").replace("　", " ")
    s = re.sub(r"^(NO\.?|NUMBER)\s*", "", s)
    if not s:
        raise PatentApiError(INVALID_INPUT, "번호가 비어 있습니다.")
    return s


def _valid_year(y: str) -> bool:
    return 1948 <= int(y) <= 2099


# ---------------------------------------------------------------------------
# 한국 출원번호 (KIPRIS 입력용)
# ---------------------------------------------------------------------------


def format_kr_application(digits13: str) -> str:
    """'1020260012345' → '10-2026-0012345'"""
    return f"{digits13[:2]}-{digits13[2:6]}-{digits13[6:]}"


def normalize_kr_application_number(raw: str) -> str:
    """한국 출원번호를 KIPRIS 형식(13자리 숫자)으로 바꾼다.

    받는 형식: '10-2026-0012345', '1020260012345', 'KR10-2026-0012345', 'KR 10-2026-0012345'
    거부하는 형식: 종류코드가 붙은 번호(공개·등록번호), 등록번호(10-1234567), 디자인·상표 번호
    """
    s = _clean(raw)
    s = re.sub(r"^KR\s*[-.]?\s*", "", s)

    # 종류코드가 붙어 있으면 공개/등록번호다.
    m_kind = re.fullmatch(rf"([0-9][0-9\s\-./]*[0-9])\s*({_KIND_RE})", s)
    if m_kind:
        raise PatentApiError(
            INVALID_INPUT,
            f"'{raw}'는 종류코드({m_kind.group(2)})가 붙은 공개·등록번호로 보입니다. "
            "kr_biblio에는 출원번호(예: 10-2026-0012345)를 넣어 주세요. "
            "공개·등록번호로 조회하려면 ep_biblio(번호에 KR 붙여서)를 쓰세요.",
        )

    if not re.fullmatch(r"[0-9\s\-./]+", s):
        raise PatentApiError(
            INVALID_INPUT,
            f"'{raw}'는 한국 출원번호 형식이 아닙니다. 예: 10-2026-0012345",
        )

    parts = [p for p in re.split(r"[\s\-./]+", s) if p]
    if len(parts) == 3:
        right, year, serial = parts
        if len(right) != 2 or len(year) != 4 or not (1 <= len(serial) <= 7):
            raise PatentApiError(
                INVALID_INPUT, f"'{raw}'는 한국 출원번호 형식이 아닙니다. 예: 10-2026-0012345"
            )
        digits = right + year + serial.zfill(7)
    else:
        digits = "".join(parts)

    right = digits[:2]
    if right in _KR_OTHER_RIGHT_CODES:
        raise PatentApiError(
            INVALID_INPUT,
            f"'{raw}'는 {_KR_OTHER_RIGHT_CODES[right]} 번호입니다. 이 도구는 특허·실용신안만 조회합니다.",
        )

    if len(digits) == 9 and right in _KR_RIGHT_CODES:
        raise PatentApiError(
            INVALID_INPUT,
            f"'{raw}'는 등록번호(예: 10-1234567)로 보입니다. kr_biblio에는 출원번호(10-YYYY-NNNNNNN)를 넣어 주세요.",
        )

    if len(digits) != 13 or right not in _KR_RIGHT_CODES or not _valid_year(digits[2:6]):
        raise PatentApiError(
            INVALID_INPUT,
            f"'{raw}'는 한국 출원번호 형식이 아닙니다. 13자리(10-YYYY-NNNNNNN)여야 합니다.",
        )
    return digits


# ---------------------------------------------------------------------------
# OPS 번호 (국가 무관)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OpsReference:
    """OPS 조회용 번호. 종류코드가 있으면 docdb, 없으면 epodoc 형식으로 보낸다."""

    ref_type: RefType
    country: str
    number: str
    kind: str | None = None
    kind_assumed: bool = False  # 입력에 없던 종류코드를 관례로 붙였으면 True

    @property
    def input_format(self) -> str:
        # 종류코드가 있으면 docdb(출원번호의 'A' 포함), 없으면 epodoc
        return "docdb" if self.kind else "epodoc"

    @property
    def ops_number(self) -> str:
        if self.kind:
            return f"{self.country}.{self.number}.{self.kind}"
        return f"{self.country}{self.number}"

    @property
    def display(self) -> str:
        return f"{self.country}{self.number}{self.kind or ''}"

    def path(self) -> str:
        """OPS URL 경로 조각: 'publication/docdb/EP.1234567.A1'"""
        return f"{self.ref_type}/{self.input_format}/{self.ops_number}"

    def to_query(self) -> dict:
        q = {"type": self.ref_type, "country": self.country, "number": self.number}
        if self.kind:
            q["kind"] = self.kind
        q["ops"] = f"{self.input_format}:{self.ops_number}"
        return q


def _expand_wo_year(y: str) -> str:
    if len(y) == 4:
        return y
    yy = int(y)
    return ("19" if yy >= 78 else "20") + y


def normalize_ops_number(raw: str, ref_type: RefType | None = None) -> OpsReference:
    """국가 무관 특허 번호를 OPS 형식으로 바꾼다.

    ref_type: None이면 공개(publication)로 보되, 번호만 보고 공개/출원을 가를 수 없으면 오류.
              'application'을 주면 출원번호로 조회한다.
    """
    if ref_type not in (None, "publication", "application"):
        raise PatentApiError(INVALID_INPUT, "number_type은 'publication' 또는 'application'이어야 합니다.")
    s = _clean(raw)

    # PCT 국제출원번호
    if re.match(r"^PCT\b", s):
        raise PatentApiError(
            INVALID_INPUT,
            f"'{raw}'는 PCT 국제출원번호입니다. 국제공개번호(예: WO2026/012345)를 넣어 주세요.",
        )

    m = re.fullmatch(rf"([A-Z]{{2}})?\s*[-.]?\s*([0-9][0-9\s\-./,]*?)\s*[-.]?\s*({_KIND_RE})?", s)
    if not m or not m.group(2):
        raise PatentApiError(
            INVALID_INPUT,
            f"'{raw}'를 특허 번호로 해석할 수 없습니다. 예: EP1234567A1, US 2026/0123456 A1, WO2026/012345, KR 10-2026-0012345 A",
        )
    country, body, kind = m.group(1), m.group(2).strip(), m.group(3)
    digits = _SEPARATORS.sub("", body)
    if not digits.isdigit():
        raise PatentApiError(INVALID_INPUT, f"'{raw}'의 번호 부분에 숫자가 아닌 문자가 있습니다.")

    if country is None:
        # 국가코드 없이도 알아볼 수 있는 건 한국 번호(10-YYYY-NNNNNNN, 10-1234567)뿐이다.
        if digits[:2] in _KR_RIGHT_CODES and len(digits) in (9, 13):
            country = "KR"
        else:
            raise PatentApiError(
                INVALID_INPUT,
                f"'{raw}'에 국가코드가 없습니다. 앞에 국가코드를 붙여 주세요. 예: EP1234567, US2026/0123456",
            )

    if kind and ref_type == "application":
        raise PatentApiError(
            INVALID_INPUT,
            f"'{raw}'에 종류코드({kind})가 있는데 출원번호로 조회하라고 했습니다. "
            "종류코드는 공개·등록번호에 붙습니다. 둘 중 하나를 고쳐 주세요.",
        )

    if country == "KR":
        return _normalize_kr_ops(raw, digits, kind, ref_type)
    if country == "WO":
        return _normalize_wo(raw, body, digits, kind, ref_type)
    if country == "EP":
        return _normalize_ep(raw, body, digits, kind, ref_type)
    if country == "US":
        return _normalize_us(raw, body, digits, kind, ref_type)
    if country == "JP":
        return _normalize_jp(raw, body, digits, kind, ref_type)

    return OpsReference(ref_type or "publication", country, digits.lstrip("0") or "0", kind)


def _normalize_kr_ops(raw: str, digits: str, kind: str | None, ref_type: RefType | None) -> OpsReference:
    right = digits[:2]
    if right in _KR_OTHER_RIGHT_CODES:
        raise PatentApiError(INVALID_INPUT, f"'{raw}'는 {_KR_OTHER_RIGHT_CODES[right]} 번호입니다. 특허·실용신안만 조회합니다.")

    if len(digits) == 13 and right in _KR_RIGHT_CODES and _valid_year(digits[2:6]):
        # 10-YYYY-NNNNNNN: 출원번호와 공개번호가 같은 모양이다.
        if kind:
            if kind[0] not in ("A", "U"):
                raise PatentApiError(
                    INVALID_INPUT,
                    f"'{raw}': 10-YYYY-NNNNNNN 모양은 공개번호인데 종류코드가 {kind}입니다. "
                    "등록번호는 10-1234567 모양입니다.",
                )
            return OpsReference("publication", "KR", digits[2:], kind)
        if ref_type is None:
            raise PatentApiError(
                AMBIGUOUS_NUMBER,
                f"'{raw}'는 한국 출원번호인지 공개번호인지 모양만으로 알 수 없습니다. "
                "공개번호라면 종류코드를 붙이고(예: KR 10-2026-0012345 A), "
                "출원번호라면 number_type='application'으로 지정해 주세요. "
                "한국 출원번호로 서지를 보려면 kr_biblio를 쓰세요.",
            )
        if ref_type == "application":
            # OPS는 한국 출원번호를 docdb 'KR.20200168607.A'(특허) 형식으로만 받는다(실측).
            # 실용신안(20-)은 종류 'U'로 본다(미실측).
            return OpsReference("application", "KR", digits[2:], "A" if right == "10" else "U")
        return OpsReference(ref_type, "KR", digits[2:], None)

    if len(digits) == 9 and right in _KR_RIGHT_CODES:
        # 10-1234567: 등록번호
        if ref_type == "application":
            raise PatentApiError(
                INVALID_INPUT, f"'{raw}'는 등록번호 모양(10-1234567)입니다. 출원번호는 10-YYYY-NNNNNNN 모양입니다."
            )
        if kind and kind[0] not in ("B", "Y"):
            raise PatentApiError(INVALID_INPUT, f"'{raw}': 등록번호 모양인데 종류코드가 {kind}입니다.")
        if not kind:
            # OPS는 한국 등록번호를 종류코드 없이(epodoc) 찾지 못한다(실측). 특허 B1, 실용신안 Y1로 본다.
            return OpsReference("publication", "KR", digits, "B1" if right == "10" else "Y1", kind_assumed=True)
        return OpsReference("publication", "KR", digits, kind)

    # 이미 docdb식으로 적은 경우: KR20260012345A, KR101234567B1
    if len(digits) == 11 and _valid_year(digits[:4]):
        if not kind and ref_type is None:
            raise PatentApiError(
                AMBIGUOUS_NUMBER,
                f"'{raw}'는 출원번호인지 공개번호인지 알 수 없습니다. 종류코드를 붙이거나 number_type을 지정해 주세요.",
            )
        return OpsReference(ref_type or "publication", "KR", digits, kind)

    raise PatentApiError(
        INVALID_INPUT,
        f"'{raw}'는 한국 번호 형식이 아닙니다. 공개번호 예: KR 10-2026-0012345 A, 등록번호 예: KR 10-1234567 B1",
    )


def _normalize_wo(raw: str, body: str, digits: str, kind: str | None, ref_type: RefType | None) -> OpsReference:
    if ref_type == "application":
        raise PatentApiError(
            INVALID_INPUT, "WO 번호는 국제공개번호입니다. PCT 출원번호로는 조회하지 않습니다. 국제공개번호를 넣어 주세요."
        )
    parts = [p for p in re.split(r"[\s\-./]+", body) if p]
    if len(parts) == 2:
        year, serial = parts
        if len(year) not in (2, 4) or len(serial) > 6:
            raise PatentApiError(INVALID_INPUT, f"'{raw}'는 WO 공개번호 형식이 아닙니다. 예: WO2026/012345")
        number = _expand_wo_year(year) + serial.zfill(6)
    elif len(digits) == 10 and _valid_year(digits[:4]):
        number = digits
    elif len(digits) == 7:
        # WO 02/12345 를 띄어쓰기 없이 0212345로 적은 경우
        number = _expand_wo_year(digits[:2]) + digits[2:].zfill(6)
    else:
        raise PatentApiError(INVALID_INPUT, f"'{raw}'는 WO 공개번호 형식이 아닙니다. 예: WO2026/012345")
    return OpsReference("publication", "WO", number, kind)


def _normalize_ep(raw: str, body: str, digits: str, kind: str | None, ref_type: RefType | None) -> OpsReference:
    # EP 출원번호: 8자리 + 점 + 검증숫자(예: 26123456.7)
    m_app = re.fullmatch(r"([0-9]{8})\s*\.\s*([0-9])", body)
    if m_app or len(digits) in (8, 9) and ref_type != "publication":
        if kind:
            raise PatentApiError(INVALID_INPUT, f"'{raw}'는 EP 출원번호 모양인데 종류코드({kind})가 붙어 있습니다.")
        if ref_type is None:
            raise PatentApiError(
                AMBIGUOUS_NUMBER,
                f"'{raw}'는 EP 출원번호(8자리)로 보입니다. 출원번호로 조회하려면 number_type='application'을, "
                "공개번호라면 7자리 번호(예: EP1234567A1)를 넣어 주세요.",
            )
        app = m_app.group(1) if m_app else digits[:8]
        # OPS는 EP 출원번호를 docdb 'EP.21900938.A' 형식으로만 받는다(epodoc은 자료 없음, 실측).
        return OpsReference("application", "EP", app, "A")
    if len(digits) > 7:
        raise PatentApiError(INVALID_INPUT, f"'{raw}'는 EP 공개번호 형식(7자리)이 아닙니다. 예: EP1234567A1")
    return OpsReference(ref_type or "publication", "EP", digits.zfill(7), kind)


def _normalize_us(raw: str, body: str, digits: str, kind: str | None, ref_type: RefType | None) -> OpsReference:
    if ref_type == "application":
        # 미국 출원번호(예: 17/123,456)는 OPS에서 형식이 까다로워 그대로 넘긴다.
        return OpsReference("application", "US", digits, None)
    parts = [p for p in re.split(r"[\s\-./]+", body) if p]
    # 공개번호 2026/0123456: OPS(docdb·epodoc)는 연도 + 일련번호 6자리(앞의 0 제외)로 적는다 → US2026123456 (실측)
    if len(parts) == 2 and len(parts[0]) == 4 and _valid_year(parts[0]) and len(parts[1]) <= 7:
        return OpsReference("publication", "US", parts[0] + str(int(parts[1])).zfill(6), kind)
    if len(digits) in (10, 11) and _valid_year(digits[:4]):
        return OpsReference("publication", "US", digits[:4] + str(int(digits[4:])).zfill(6), kind)
    if len(digits) <= 8:
        # 등록번호 10,123,456 / 7654321
        return OpsReference("publication", "US", digits.lstrip("0"), kind)
    raise PatentApiError(
        INVALID_INPUT,
        f"'{raw}'는 미국 번호 형식이 아닙니다. 공개번호 예: US 2026/0123456 A1, 등록번호 예: US 10,123,456 B2",
    )


def _normalize_jp(raw: str, body: str, digits: str, kind: str | None, ref_type: RefType | None) -> OpsReference:
    parts = [p for p in re.split(r"[\s\-./]+", body) if p]
    if len(parts) == 2 and len(parts[0]) == 4 and _valid_year(parts[0]):
        return OpsReference(ref_type or "publication", "JP", parts[0] + parts[1].zfill(6), kind)
    return OpsReference(ref_type or "publication", "JP", digits, kind)
