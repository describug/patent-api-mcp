"""KIPRIS Plus(특허·실용신안 정보 서비스) 호출·파싱.

게이트웨이: https://plus.kipris.or.kr/kipo-api/kipi/patUtiModInfoSearchSevice
  (서비스명의 'Sevice' 오타는 공식 이름이다)
- getBibliographyDetailInfoSearch : 출원번호 → 서지상세
- getAdvancedSearch               : 항목별 검색
인증 파라미터: ServiceKey
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any, Callable

import httpx

from . import errors as E
from .errors import PatentApiError
from .http import request_with_retry
from .numbers import format_kr_application

SOURCE = "KIPRIS"

# resultCode → (표준 코드, 설명). 공공데이터 표준 코드 + KIPRIS Plus 실측 동작.
RESULT_CODES: dict[str, tuple[str, str]] = {
    "01": (E.UPSTREAM_ERROR, "KIPRIS 서버 내부 오류(어플리케이션 에러)"),
    "02": (E.UPSTREAM_ERROR, "KIPRIS 서버 데이터베이스 오류"),
    "04": (E.UPSTREAM_ERROR, "KIPRIS HTTP 오류"),
    "05": (E.UPSTREAM_UNAVAILABLE, "KIPRIS 서비스 연결 실패(일시 장애일 수 있음)"),
    "10": (E.UPSTREAM_BAD_REQUEST, "요청 파라미터가 잘못되었습니다"),
    "11": (E.UPSTREAM_BAD_REQUEST, "필수 요청 파라미터가 없습니다"),
    "12": (E.UPSTREAM_BAD_REQUEST, "해당 오픈API 서비스가 없거나 폐기되었습니다"),
    "20": (E.PERMISSION_DENIED, "서비스 접근이 거부되었습니다. KIPRIS Plus에서 해당 기능(오퍼레이션)의 활용신청이 승인됐는지 확인하세요"),
    "22": (E.QUOTA_EXCEEDED, "KIPRIS 서비스 요청 제한 횟수를 초과했습니다"),
    "30": (E.AUTH_FAILED, "등록되지 않은 서비스키입니다. 넣은 KIPRIS 인증키(KIPRIS_SERVICE_KEY)가 마이페이지 > API KEY 관리의 값과 같은지, 이 기능의 Open API 신청이 되어 있는지 확인하세요"),
    "31": (E.AUTH_FAILED, "이 키로 쓸 수 있는 상품의 이용기간이 없거나 끝났습니다. KIPRIS Plus에서 '특허·실용 공개·등록공보' Open API를 신청(무료 플랜 가능)했는지, 마이페이지 > 서비스 구매내역의 이용기간을 확인하세요. 이용기간은 그해 12월 31일까지만 잡히므로 해가 바뀌면 다시 신청해야 합니다"),
    "32": (E.PERMISSION_DENIED, "등록되지 않은 IP에서 호출했습니다. KIPRIS Plus에 등록한 IP를 확인하세요"),
    "33": (E.AUTH_FAILED, "서명되지 않은 호출입니다"),
    "99": (E.UPSTREAM_ERROR, "KIPRIS 기타 오류"),
    # openapi/rest 게이트웨이: 키가 틀렸거나 그 상품을 신청하지 않았을 때 모두 101을 준다(2026-10 실측).
    "101": (E.PERMISSION_DENIED, "이 인증키로 해당 상품을 쓸 수 없습니다. KIPRIS Plus에서 그 상품을 신청했는지, 인증키가 맞는지 확인하세요"),
}
# 상품을 신청하지 않았거나(이용기간 없음) 접근이 거부된 경우의 resultCode.
# kipo-api는 미신청 상품에 31(DEADLINE_HAS_EXPIRED_ERROR), openapi/rest는 101을 준다(2026-10 실측).
NOT_SUBSCRIBED_CODES = {"20", "30", "31", "101"}
NO_DATA_CODES = {"03"}  # NODATA_ERROR
_NO_DATA_MSG = re.compile(r"(결과.*없|없습니다|NO[\s_]?DATA|NOT\s?FOUND)", re.I)

# data.go.kr 게이트웨이식 인증 오류(returnAuthMsg)
_AUTH_MSGS = {
    "SERVICE_KEY_IS_NOT_REGISTERED_ERROR": "30",
    "DEADLINE_HAS_EXPIRED_ERROR": "31",
    "UNREGISTERED_IP_ERROR": "32",
    "LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR": "22",
    "SERVICE_ACCESS_DENIED_ERROR": "20",
    "NO_OPENAPI_SERVICE_ERROR": "12",
}


# ---------------------------------------------------------------------------
# 순수 파싱 함수 (네트워크 없음, 테스트 대상)
# ---------------------------------------------------------------------------


def normalize_date(value: str | None) -> str | None:
    """'20260115', '2026.01.15', '2026-01-15', '2026/01/15' → '2026-01-15'"""
    if not value:
        return None
    v = value.strip()
    digits = re.sub(r"\D", "", v)
    if len(digits) >= 8:
        y, m, d = digits[:4], digits[4:6], digits[6:8]
        return f"{y}-{m}-{d}"
    return v or None


def format_kr_number(value: str | None) -> str | None:
    """KIPRIS가 돌려준 번호를 사람이 읽는 모양으로. 13자리 → 10-YYYY-NNNNNNN, 9자리 → 10-NNNNNNN"""
    if not value:
        return None
    v = value.strip()
    d = re.sub(r"\D", "", v)
    if len(d) == 13:
        return format_kr_application(d)
    if len(d) == 9:
        return f"{d[:2]}-{d[2:]}"
    return v or None


def format_kr_register_number(value: str | None) -> str | None:
    """등록·공고번호. KIPRIS는 '1026543210000'(10 + 7자리 + 0000)으로 준다 → '10-2654321'"""
    if not value:
        return None
    d = re.sub(r"\D", "", value)
    if len(d) == 13 and d.endswith("0000"):
        return f"{d[:2]}-{d[2:9]}"
    return format_kr_number(value)


def _text(el: ET.Element | None, tag: str) -> str | None:
    if el is None:
        return None
    found = el.find(tag)
    if found is None or found.text is None:
        return None
    t = found.text.strip()
    return t or None


def _all_text(el: ET.Element | None) -> str | None:
    if el is None:
        return None
    t = " ".join(s.strip() for s in el.itertext() if s and s.strip())
    return t or None


def parse_xml(text: str) -> ET.Element:
    stripped = text.strip()
    if not stripped:
        raise PatentApiError(E.UPSTREAM_ERROR, "KIPRIS가 빈 응답을 돌려줬습니다(일시 장애일 수 있음).", retryable=True)
    head = stripped[:200].lower()
    if head.startswith("<!doctype html") or head.startswith("<html"):
        raise PatentApiError(
            E.UPSTREAM_UNAVAILABLE, "KIPRIS가 XML 대신 HTML 오류 페이지를 돌려줬습니다(점검·일시 장애일 수 있음).",
            retryable=True,
        )
    try:
        return ET.fromstring(stripped.encode("utf-8"))
    except ET.ParseError as e:
        raise PatentApiError(E.PARSE_ERROR, f"KIPRIS 응답 XML을 해석하지 못했습니다: {e}") from None


def check_header(root: ET.Element) -> bool:
    """헤더를 검사한다. 정상이면 False, '자료 없음'이면 True, 오류면 PatentApiError."""
    # data.go.kr 게이트웨이식 오류
    auth_msg = root.findtext(".//returnAuthMsg")
    if auth_msg:
        code = root.findtext(".//returnReasonCode") or _AUTH_MSGS.get(auth_msg.strip(), "99")
        std, desc = RESULT_CODES.get(code.strip(), (E.UPSTREAM_ERROR, "KIPRIS 오류"))
        raise PatentApiError(std, f"{desc} (KIPRIS {code.strip()} {auth_msg.strip()})", upstream_code=code.strip())

    code = (root.findtext(".//header/resultCode") or root.findtext(".//resultCode") or "").strip()
    msg = (root.findtext(".//header/resultMsg") or root.findtext(".//resultMsg") or "").strip()
    success = (root.findtext(".//header/successYN") or "").strip().upper()

    if code in ("", "00", "0") and success in ("", "Y"):
        if code == "" and success == "" and root.find(".//body") is None and root.find(".//items") is None:
            raise PatentApiError(E.PARSE_ERROR, "KIPRIS 응답에 결과 코드가 없습니다(형식이 바뀌었을 수 있음).")
        return False
    if code in NO_DATA_CODES:
        return True
    if code == "20" and _NO_DATA_MSG.search(msg):
        return True
    if code in ("", "00") and success == "N":
        if _NO_DATA_MSG.search(msg):
            return True
        raise PatentApiError(E.UPSTREAM_ERROR, f"KIPRIS가 실패를 알렸습니다: {msg or '사유 없음'}")
    std, desc = RESULT_CODES.get(code, (E.UPSTREAM_ERROR, "KIPRIS 오류"))
    detail = f" — {msg}" if msg else ""
    raise PatentApiError(std, f"{desc} (KIPRIS resultCode {code}{detail})", upstream_code=code)


def _items(root: ET.Element, array_tag: str, item_tag: str) -> list[ET.Element]:
    arr = root.find(f".//{array_tag}")
    if arr is None:
        return []
    return arr.findall(item_tag)


def _person(el: ET.Element) -> dict:
    return _drop_none(
        {
            "name": _text(el, "name"),
            "nameEng": _text(el, "engName"),
            "country": _text(el, "country"),
            "code": _text(el, "code"),
        }
    )


def _drop_none(d: dict) -> dict:
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


def parse_biblio_detail(xml_text: str) -> dict | None:
    """getBibliographyDetailInfoSearch 응답 → dict. 자료가 없으면 None."""
    root = parse_xml(xml_text)
    if check_header(root):
        return None
    summary = root.find(".//biblioSummaryInfoArray/biblioSummaryInfo")
    if summary is None:
        summary = root.find(".//biblioSummaryInfo")
    if summary is None:
        return None

    def s(tag: str) -> str | None:
        return _text(summary, tag)

    data: dict[str, Any] = {
        "applicationNumber": format_kr_number(s("applicationNumber")),
        "applicationDate": normalize_date(s("applicationDate")),
        "inventionTitle": s("inventionTitle"),
        "inventionTitleEng": s("inventionTitleEng"),
        "status": s("registerStatus"),
        "finalDisposal": s("finalDisposal"),
        "examinerName": s("examinerName"),
        "claimCount": s("claimCount"),
        "openNumber": format_kr_number(s("openNumber")),
        "openDate": normalize_date(s("openDate")),
        "publicationNumber": format_kr_register_number(s("publicationNumber")),
        "publicationDate": normalize_date(s("publicationDate")),
        "registerNumber": format_kr_register_number(s("registerNumber")),
        "registerDate": normalize_date(s("registerDate")),
        "originalApplication": _drop_none(
            {
                "kind": s("originalApplicationKind"),
                "number": format_kr_number(s("originalApplicationNumber")),
                "date": normalize_date(s("originalApplicationDate")),
            }
        ),
        "examinationRequest": _drop_none(
            {
                "flag": s("originalExaminationRequestFlag"),
                "date": normalize_date(s("originalExaminationRequestDate")),
            }
        ),
        "applicants": [_person(x) for x in _items(root, "applicantInfoArray", "applicantInfo")],
        "inventors": [_person(x) for x in _items(root, "inventorInfoArray", "inventorInfo")],
        "agents": [_person(x) for x in _items(root, "agentInfoArray", "agentInfo")],
        "ipc": [
            _drop_none({"code": _text(x, "ipcNumber"), "date": normalize_date(_text(x, "ipcDate"))})
            for x in _items(root, "ipcInfoArray", "ipcInfo")
        ],
        "priorities": [
            _drop_none(
                {
                    "country": _text(x, "priorityApplicationCountry"),
                    "number": _text(x, "priorityApplicationNumber"),
                    "date": normalize_date(_text(x, "priorityApplicationDate")),
                }
            )
            for x in _items(root, "priorityInfoArray", "priorityInfo")
        ],
        "international": [
            _drop_none(
                {
                    "applicationNumber": _text(x, "internationalApplicationNumber"),
                    "applicationDate": normalize_date(_text(x, "internationalApplicationDate")),
                    "openNumber": _text(x, "internationOpenNumber"),
                    "openDate": normalize_date(_text(x, "internationOpenDate")),
                }
            )
            for x in _items(root, "internationalInfoArray", "internationalInfo")
        ],
        "priorArt": [
            _drop_none(
                {
                    "number": _text(x, "documentsNumber"),
                    "examinerCited": _text(x, "examinerQuotationFlag"),
                }
            )
            for x in _items(root, "priorArtDocumentsInfoArray", "priorArtDocumentsInfo")
        ],
        "familyApplicationNumbers": [
            t for t in (_text(x, "familyApplicationNumber") for x in _items(root, "familyInfoArray", "familyInfo")) if t
        ],
        "abstract": " ".join(
            t for t in (_all_text(x.find("astrtCont")) for x in _items(root, "abstractInfoArray", "abstractInfo")) if t
        )
        or None,
        "claims": [
            t for t in (_all_text(x.find("claim")) for x in _items(root, "claimInfoArray", "claimInfo")) if t
        ],
        "history": [
            _drop_none(
                {
                    "date": normalize_date(_text(x, "receiptDate")),
                    "document": _text(x, "documentName"),
                    "status": _text(x, "commonCodeName"),
                    "receiptNumber": _text(x, "receiptNumber"),
                }
            )
            for x in _items(root, "legalStatusInfoArray", "legalStatusInfo")
        ],
    }
    data["applicants"] = [p for p in data["applicants"] if p]
    data["inventors"] = [p for p in data["inventors"] if p]
    data["agents"] = [p for p in data["agents"] if p]
    data["ipc"] = [p for p in data["ipc"] if p]
    data["priorities"] = [p for p in data["priorities"] if p]
    data["international"] = [p for p in data["international"] if p]
    data["priorArt"] = [p for p in data["priorArt"] if p]
    data["history"] = [p for p in data["history"] if p]
    return {k: v for k, v in data.items() if v not in (None, "", {}, [])}


def parse_search(xml_text: str) -> dict | None:
    """getAdvancedSearch 응답 → {'totalCount', 'items'}. 결과가 없으면 None."""
    root = parse_xml(xml_text)
    if check_header(root):
        return None
    items = []
    for it in root.iter("item"):
        if it.find("applicationNumber") is None and it.find("inventionTitle") is None:
            continue
        items.append(
            _drop_none(
                {
                    "applicationNumber": format_kr_number(_text(it, "applicationNumber")),
                    "applicationDate": normalize_date(_text(it, "applicationDate")),
                    "inventionTitle": _text(it, "inventionTitle"),
                    "applicant": _text(it, "applicantName"),
                    "ipc": _text(it, "ipcNumber"),
                    "status": _text(it, "registerStatus"),
                    "openNumber": format_kr_number(_text(it, "openNumber")),
                    "openDate": normalize_date(_text(it, "openDate")),
                    "publicationNumber": format_kr_register_number(_text(it, "publicationNumber")),
                    "publicationDate": normalize_date(_text(it, "publicationDate")),
                    "registerNumber": format_kr_register_number(_text(it, "registerNumber")),
                    "registerDate": normalize_date(_text(it, "registerDate")),
                    "abstract": _all_text(it.find("astrtCont")),
                }
            )
        )
    total_raw = root.findtext(".//totalCount") or root.findtext(".//TotalSearchCount")
    try:
        total = int(total_raw) if total_raw else len(items)
    except ValueError:
        total = len(items)
    if total == 0 and not items:
        return None
    return {"totalCount": total, "items": items}


# ---------------------------------------------------------------------------
# 호출
# ---------------------------------------------------------------------------


class KiprisClient:
    def __init__(
        self,
        service_key: str | None,
        *,
        base_url: str,
        http: httpx.AsyncClient,
        on_call: Callable[[], Any] | None = None,
        key_hint: str = ".env 파일에",
        rest_base_url: str | None = None,
    ):
        self._key = service_key
        self._base = base_url.rstrip("/")
        # kipo-api 게이트웨이 뿌리(…/kipo-api/kipi)와 openapi/rest 게이트웨이
        self._kipi_root = self._base.rsplit("/", 1)[0]
        self._rest_base = (rest_base_url or "https://plus.kipris.or.kr/openapi/rest").rstrip("/")
        self._http = http
        self._on_call = on_call
        self._key_hint = key_hint

    def require_key(self) -> None:
        if not self._key:
            raise PatentApiError(
                E.CONFIG_MISSING_KEY,
                f"KIPRIS_SERVICE_KEY가 설정되지 않았습니다. {self._key_hint} KIPRIS Plus 인증키를 넣고 "
                "Claude를 다시 시작하세요.",
            )

    async def _get(self, operation: str, params: dict[str, Any]) -> str:
        return await self._request(f"{self._base}/{operation}", params, "ServiceKey")

    async def kipi(self, service: str, operation: str, params: dict[str, Any]) -> str:
        """kipo-api 게이트웨이(인증 파라미터 ServiceKey)의 다른 서비스를 부른다. 응답 XML을 돌려준다."""
        return await self._request(f"{self._kipi_root}/{service}/{operation}", params, "ServiceKey")

    async def rest(self, service: str, operation: str, params: dict[str, Any]) -> str:
        """openapi/rest 게이트웨이(인증 파라미터 accessKey)를 부른다. 응답 XML을 돌려준다."""
        return await self._request(f"{self._rest_base}/{service}/{operation}", params, "accessKey")

    async def _request(self, url: str, params: dict[str, Any], auth_param: str) -> str:
        self.require_key()
        query = {k: v for k, v in params.items() if v not in (None, "")}
        query[auth_param] = self._key
        resp = await request_with_retry(self._http, "GET", url, source=SOURCE, params=query, on_send=self._on_call)
        if resp.status_code in (401, 403):
            raise PatentApiError(
                E.AUTH_FAILED,
                f"KIPRIS가 요청을 거부했습니다(HTTP {resp.status_code}). KIPRIS 인증키(KIPRIS_SERVICE_KEY)를 확인하세요.",
            )
        if resp.status_code == 429:
            raise PatentApiError(E.RATE_LIMITED, "KIPRIS 호출이 너무 잦습니다(HTTP 429). 잠시 뒤 다시 시도하세요.")
        if resp.status_code >= 500:
            raise PatentApiError(
                E.UPSTREAM_UNAVAILABLE, f"KIPRIS 서버 오류(HTTP {resp.status_code}). 잠시 뒤 다시 시도하세요.", retryable=True
            )
        if resp.status_code >= 400:
            raise PatentApiError(E.UPSTREAM_BAD_REQUEST, f"KIPRIS가 요청을 받지 않았습니다(HTTP {resp.status_code}).")
        return resp.text

    async def biblio_detail(self, application_number13: str) -> dict | None:
        xml_text = await self._get("getBibliographyDetailInfoSearch", {"applicationNumber": application_number13})
        return parse_biblio_detail(xml_text)

    async def advanced_search(self, params: dict[str, Any]) -> dict | None:
        xml_text = await self._get("getAdvancedSearch", params)
        return parse_search(xml_text)
