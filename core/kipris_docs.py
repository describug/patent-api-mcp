"""KIPRIS Plus 추가 상품(심사 서류·마감기한·청구항 이력·등록사항·인용·법적 상태·패밀리) 응답 파싱.

모두 네트워크 없는 순수 함수다. 자료가 없으면 None을 돌려준다(오류와 구분).
필드 구조는 KIPRIS Plus '데이터 목록 > API' 상품 페이지와 2026-10 실제 응답(tests/fixtures/real_kipris_*.xml)을 따른다.
"""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from typing import Any

from .kipris import check_header, format_kr_number, format_kr_register_number, normalize_date, parse_xml

# ---------------------------------------------------------------------------
# 공통
# ---------------------------------------------------------------------------


def _val(el: ET.Element | None, tag: str) -> str | None:
    """하위 태그 값. KIPRIS는 빈 값을 ' '로 주므로 공백만 있으면 None."""
    if el is None:
        return None
    found = el.find(tag)
    if found is None:
        return None
    t = "".join(found.itertext()).strip()
    return t or None


def _drop_none(d: dict) -> dict:
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


def _root(xml_text: str) -> ET.Element | None:
    """XML을 읽고 헤더를 검사한다. '자료 없음'이면 None."""
    root = parse_xml(xml_text)
    if check_header(root):
        return None
    return root


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


_BR = re.compile(r"<\s*br\s*/?\s*>", re.I)
_P_BREAK = re.compile(r"</\s*p\s*>\s*<\s*p[^>]*>", re.I)
_TAG = re.compile(r"<[^>]+>")


def clean_text(value: str | None) -> str | None:
    """서류 본문의 <BR>, <p> 등을 줄바꿈으로 바꾸고 나머지 태그를 걷어낸다."""
    if value is None:
        return None
    t = _P_BREAK.sub("\n", value)
    t = _BR.sub("\n", t)
    t = _TAG.sub("", t)
    t = html.unescape(t).replace("\xa0", " ")
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    return t.strip() or None


def clean_diff(value: str | None) -> str | None:
    """청구항 변동 표시: <Del>x</Del> → [-x-], <Ins>x</Ins> → {+x+}."""
    if value is None:
        return None
    t = re.sub(r"<\s*Del\s*>(.*?)<\s*/\s*Del\s*>", r"[-\1-]", value, flags=re.S | re.I)
    t = re.sub(r"<\s*Ins\s*>(.*?)<\s*/\s*Ins\s*>", r"{+\1+}", t, flags=re.S | re.I)
    return clean_text(t)


# ---------------------------------------------------------------------------
# 심사 서류: 의견제출통지서(IntermediateDocumentOPService) · 거절결정서(…REService) · 등록결정서(…RGService)
# ---------------------------------------------------------------------------


def parse_doc_biblio(xml_text: str) -> list[dict] | None:
    """bibliographicInfo → 서류(발송번호)별 서지. 세 상품 공통."""
    root = _root(xml_text)
    if root is None:
        return None
    docs = []
    for it in root.iter("bibliographicInfo"):
        docs.append(
            _drop_none(
                {
                    "sendNumber": _val(it, "sendNumber"),
                    "sendDate": normalize_date(_val(it, "sendDate")),
                    "document": _val(it, "documentName"),
                    "submitDueDate": normalize_date(_val(it, "submitDuedate")),
                    "drawupDate": normalize_date(_val(it, "documentDrawupDate")),
                    "inventionTitle": _val(it, "inventionTitle") or _val(it, "inventionName"),
                    "claimCount": _int(_val(it, "demandItemcount")),
                }
            )
        )
    docs = [d for d in docs if d.get("sendNumber")]
    return docs or None


def parse_refusal_table(value: str | None) -> list[dict]:
    """'순번†거절이유가 있는 부분†관련 법조항§1†청구항 전항†특허법 제42조제4항제2호' → 행 목록."""
    text = clean_text(value)
    if not text:
        return []
    rows = []
    for line in text.split("§"):
        cells = [c.strip() for c in line.split("†")]
        if len(cells) < 3 or not cells[0] or cells[0] == "순번":
            continue
        rows.append(_drop_none({"no": cells[0], "part": cells[1], "law": "†".join(cells[2:]).strip()}))
    return rows


def parse_exam_result(xml_text: str) -> dict[str, dict] | None:
    """examineResultInfo → {발송번호: {심사 대상 청구항, 거절이유 표, 특허 가능 청구항}}"""
    root = _root(xml_text)
    if root is None:
        return None
    out: dict[str, dict] = {}
    for it in root.iter("examineResultInfo"):
        send = _val(it, "sendNumber")
        if not send:
            continue
        out[send] = _drop_none(
            {
                "examinedClaims": clean_text(_val(it, "examinationContent")),
                "refusalTable": parse_refusal_table(_val(it, "refusalLawTabularstatement")),
                "allowableClaims": clean_text(_val(it, "patentAbleContent")),
                "summary": clean_text(_val(it, "descriptionSummary")),
            }
        )
    return out or None


def parse_reject_decision(xml_text: str) -> dict[str, dict] | None:
    """rejectDecisionInfo → {발송번호: {통지 문구, 이유 제목, 이유(옛 서류는 여기에 본문), 첨부}}. 안내문은 뺀다."""
    root = _root(xml_text)
    if root is None:
        return None
    out: dict[str, dict] = {}
    for it in root.iter("rejectDecisionInfo"):
        send = _val(it, "sendNumber")
        if not send:
            continue
        reasons = "\n".join(
            t for t in (clean_text(_val(it, tag)) for tag in ("rejectionContentDetail", "lawContentDetail", "lawContentNumber")) if t
        )
        out[send] = _drop_none(
            {
                "notice": clean_text(_val(it, "lawContent")),
                "reasonsTitle": clean_text(_val(it, "rejectionContentTitle")),
                "reasons": reasons or None,
                "attachment": clean_text(_val(it, "attachmentfileContent")),
            }
        )
    return out or None


def parse_addition_reject(xml_text: str) -> dict[str, list[str]] | None:
    """additionRejectInfo → {발송번호: [구체적 거절이유 문단(순번 순)]}"""
    root = _root(xml_text)
    if root is None:
        return None
    grouped: dict[str, list[tuple[int, str]]] = {}
    for it in root.iter("additionRejectInfo"):
        send = _val(it, "sendNumber")
        text = clean_text(_val(it, "additionRejectionContent"))
        if not send or not text:
            continue
        grouped.setdefault(send, []).append((_int(_val(it, "sequence")) or 0, text))
    out = {k: [t for _, t in sorted(v, key=lambda x: x[0])] for k, v in grouped.items()}
    return out or None


def parse_allowance_content(xml_text: str) -> dict[str, list[dict]] | None:
    """등록결정서 contentInfo → {발송번호: [{구분, 내용, 직권보정 항목…}]}. '안내내용'(정형 안내문)은 뺀다."""
    root = _root(xml_text)
    if root is None:
        return None
    out: dict[str, list[dict]] = {}
    for it in root.iter("contentInfo"):
        send = _val(it, "sendNumber")
        kind = _val(it, "contentType")
        if not send or kind == "안내내용":
            continue
        entry = _drop_none(
            {
                "type": kind,
                "text": clean_text(_val(it, "content")),
                "amendmentItem": clean_text(_val(it, "amendmentItem")),
                "amendmentLocation": clean_text(_val(it, "amendmentLocation")),
                "before": clean_text(_val(it, "amendmentBeforeContent")),
                "after": clean_text(_val(it, "amendmentAfterContent")),
                "cause": clean_text(_val(it, "amendmentCause")),
            }
        )
        if entry:
            out.setdefault(send, []).append(entry)
    return out or None


_CITED = re.compile(
    r"((?:인용|선행)(?:발명|고안|디자인|문헌|기술))\s*(\d{1,2})\s*[:：]\s*([^\n]{1,200}?\)|[^\n]{1,160})"
)


def extract_cited_references(text: str | None) -> list[dict]:
    """거절이유 본문에서 '인용발명 1 : 한국 공개특허공보 제10-…호(2012.09.20.)' 같은 줄을 뽑는다(최선 노력)."""
    if not text:
        return []
    seen: set[tuple[str, str]] = set()
    refs = []
    for m in _CITED.finditer(text):
        label = f"{m.group(1)} {m.group(2)}"
        doc = m.group(3).strip(" ,.;")
        key = (label, doc)
        if key in seen or not doc:
            continue
        seen.add(key)
        refs.append({"label": label, "document": doc})
    return refs


# ---------------------------------------------------------------------------
# 통지서 마감기한 (DueDateService)
# ---------------------------------------------------------------------------


def parse_due_dates(xml_text: str) -> list[dict] | None:
    root = _root(xml_text)
    if root is None:
        return None
    out = []
    for el in root.iter():
        if el.find("ExpirationDuedateDate") is None:
            continue
        out.append(
            _drop_none(
                {
                    "applicationNumber": format_kr_number(_val(el, "ApplicationNumber")),
                    "trialNumber": _val(el, "TrialNumber"),
                    "sendNumber": _val(el, "Mailingnumber"),
                    "document": _val(el, "DocNm"),
                    "dueDate": normalize_date(_val(el, "ExpirationDuedateDate")),
                }
            )
        )
    out = [d for d in out if d.get("dueDate") or d.get("document")]
    return out or None


# ---------------------------------------------------------------------------
# 청구항 변동 이력 (ClaimsChangeHistoryService)
# ---------------------------------------------------------------------------


def parse_claim_history_order(xml_text: str) -> list[dict] | None:
    """amendmentHistoryInfo → 청구항이 바뀐 서류 순서(출원서, 보정서 …)"""
    root = _root(xml_text)
    if root is None:
        return None
    out = []
    for it in root.iter("amendmentHistoryInfo"):
        out.append(
            _drop_none(
                {
                    "version": _int(_val(it, "receiptSendSerialNumber")),
                    "receiptNumber": _val(it, "receiptSendNumber"),
                    "date": normalize_date(_val(it, "receiptSendDate")),
                    "document": _val(it, "receiptSendDocumentName"),
                }
            )
        )
    out = [d for d in out if d.get("version") is not None]
    out.sort(key=lambda d: d["version"])
    return out or None


def parse_claim_history_detail(xml_text: str) -> list[dict] | None:
    """amendmentHistoryDetailInfo → 판(서류)·청구항별 변경 내용"""
    root = _root(xml_text)
    if root is None:
        return None
    out = []
    for it in root.iter("amendmentHistoryDetailInfo"):
        out.append(
            _drop_none(
                {
                    "version": _int(_val(it, "receiptSendSerialNumber")),
                    "receiptNumber": _val(it, "receiptSendNumber"),
                    "claim": _int(_val(it, "petitionclauseNumber")),
                    "change": _val(it, "changeTypeName"),
                    "changeCode": _val(it, "changeTypeCode"),
                    "text": clean_text(_val(it, "petitionclause")),
                    "previousReceiptNumber": _val(it, "transferReceiptDocNumber"),
                    "diff": clean_diff(_val(it, "transferPetitionclause")),
                }
            )
        )
    out = [d for d in out if d.get("version") is not None and d.get("claim") is not None]
    out.sort(key=lambda d: (d["version"], d["claim"]))
    return out or None


# ---------------------------------------------------------------------------
# 등록사항 (RegistrationService/registrationInfo)
# ---------------------------------------------------------------------------


def parse_registration(xml_text: str) -> dict | None:
    root = _root(xml_text)
    if root is None:
        return None
    info = root.find(".//registrationInfo")
    if info is None:
        return None
    right = info.find("registrationRightInfo")
    if right is None and info.find("registrationRightRankInfo") is None:
        return None

    def r(tag: str) -> str | None:
        return _val(right, tag)

    holders_a = info.findall("registrationRightHolderInfo/registrationRightHolderInfoA")
    holders_b = info.findall("registrationRightHolderInfo/registrationRightHolderInfoB")
    fees = [
        _drop_none(
            {
                "fromAnnual": _int(_val(f, "startAnnual")),
                "toAnnual": _int(_val(f, "lastAnnual")),
                "paidDate": normalize_date(_val(f, "paymentDate")),
                "amount": _int(_val(f, "paymentFee")),
                "installment": _int(_val(f, "paymentDegree")),
            }
        )
        for f in info.findall("registrationFeeInfo")
    ]
    fees = [f for f in fees if f]
    paid_through = max((f["toAnnual"] for f in fees if f.get("toAnnual")), default=None)
    data = {
        "registrationNumber": format_kr_register_number(r("registrationNumber")),
        "registrationDate": normalize_date(r("registrationDate")),
        "decisionDate": normalize_date(r("assessmentDate")),
        "expirationDate": normalize_date(r("expirationDate")),
        "terminationCause": r("terminationCauseName"),
        "terminationDate": normalize_date(r("terminationDate")),
        "applicationNumber": format_kr_number(r("applicationNumber")),
        "applicationDate": normalize_date(r("applicationDate")),
        "publicationNumber": format_kr_register_number(r("publicationNumber")),
        "publicationDate": normalize_date(r("publicationDate")),
        "originalApplicationNumber": format_kr_number(r("originalApplicationNumber")),
        "inventionTitle": r("titleOfInvention"),
        "inventionTitleEng": r("titleOfInventionEng"),
        "ipc": r("classCode"),
        "claimCount": _int(r("claimCount")),
        "currentRightHolders": [
            _drop_none({"name": _val(h, "lastRightHolderName"), "country": _val(h, "lastRightHolderCountry")})
            for h in info.findall("registrationLastRightHolderInfo")
        ],
        "rightHolders": [
            _drop_none({"rank": _int(_val(h, "rankNumber")), "type": _val(h, "rankCorrelatorType"), "name": _val(h, "rankCorrelatorName")})
            for h in holders_a
        ],
        "holderChanges": [
            _drop_none(
                {
                    "rank": _int(_val(h, "rankNumber")),
                    "document": _val(h, "documentName"),
                    "receiptDate": normalize_date(_val(h, "receiptDate")),
                    "cause": _val(h, "registrationCauseName"),
                }
            )
            for h in holders_b
        ],
        "registerEntries": [
            _drop_none(
                {
                    "rank": _int(_val(e, "rankNumber")),
                    "section": _val(e, "pertinentPartition"),
                    "document": _val(e, "documentName"),
                    "purpose": _val(e, "registrationPurpose"),
                    "cause": _val(e, "registrationCauseName"),
                    "causeDate": normalize_date(_val(e, "registrationCauseDate")),
                    "registrationDate": normalize_date(_val(e, "registrationDate")),
                    "erased": True if _val(e, "disappearanceFlag") == "Y" else None,
                    "erasedCause": _val(e, "disappearanceCauseName"),
                    "erasedDate": normalize_date(_val(e, "disappearanceDate")),
                }
            )
            for e in info.findall("registrationRightRankInfo")
        ],
        "annualFees": fees,
        "paidThroughAnnual": paid_through,
    }
    for k in ("currentRightHolders", "rightHolders", "holderChanges", "registerEntries"):
        data[k] = [x for x in data[k] if x]
    return _drop_none(data) or None


# ---------------------------------------------------------------------------
# 인용문헌(CitationService/citationInfoV3) · 피인용문헌(CitingService/citingInfo)
# ---------------------------------------------------------------------------


def parse_citations(xml_text: str) -> list[dict] | None:
    root = _root(xml_text)
    if root is None:
        return None
    out = []
    for it in root.iter("citationInfoV3"):
        out.append(
            _drop_none(
                {
                    "number": _val(it, "OriginalcitationLiteraturenumber"),
                    "country": _val(it, "StandardCitationLiteratureCountryCode"),
                    "standardNumber": _val(it, "StandardCitationLiteraturenumber"),
                    "kind": _val(it, "StandardCitationIdentificationCode"),
                    "publicationDate": normalize_date(_val(it, "StandardCitationLiteraturePublicationDate")),
                    "type": _val(it, "CitationLiteratureTypeCodeName"),
                    "standardized": _val(it, "StandardStatusCodeName"),
                }
            )
        )
    out = [d for d in out if d.get("number") or d.get("standardNumber")]
    return out or None


def parse_citing(xml_text: str) -> list[dict] | None:
    root = _root(xml_text)
    if root is None:
        return None
    out = []
    for it in root.iter("citingInfo"):
        out.append(
            _drop_none(
                {
                    "applicationNumber": format_kr_number(_val(it, "ApplicationNumber")),
                    "type": _val(it, "CitationLiteratureTypeCodeName"),
                }
            )
        )
    out = [d for d in out if d.get("applicationNumber")]
    return out or None


# ---------------------------------------------------------------------------
# 법적 상태 이력 ST.27 (legStatusST27InfoSearchService/BasicInfo)
# ---------------------------------------------------------------------------

# WIPO ST.27 주요 이벤트 범주(첫 글자). KIPRIS 오퍼레이션 이름(출원(A), 등록전·후심리(E·L·W),
# IP권리존속기간이후의보호(G), IP권리중단(H), 문서수정(P), 납부(U))과 실측(Q=출원공개, D=심사)을 따른다.
ST27_CATEGORIES = {
    "A": "출원",
    "D": "심사",
    "E": "등록 전 심리",
    "F": "등록",
    "G": "존속기간 이후 보호(연장 등)",
    "H": "권리 중단(소멸 등)",
    "L": "등록 후 심리",
    "P": "문서 수정",
    "Q": "공개",
    "U": "납부",
    "W": "심리",
}


def parse_st27(xml_text: str) -> dict | None:
    root = _root(xml_text)
    if root is None:
        return None
    rows = list(root.iter("legalStatusST27Info"))
    if not rows:
        return None
    first = rows[0]
    events = []
    for it in rows:
        key = _val(it, "keyEventCode")
        nat = _val(it, "nationalEventCode")
        prev, cur = _val(it, "previousStageCode"), _val(it, "currentStageCode")
        events.append(
            _drop_none(
                {
                    "seq": _int(_val(it, "supplySerialNumber")),
                    "date": normalize_date(_val(it, "eventDate")),
                    "category": ST27_CATEGORIES.get((key or " ")[0]),
                    "keyEvent": key,
                    "detailEvent": _val(it, "detailLawEventCode"),
                    "indicator": _val(it, "eventIndicatorCode"),
                    "nationalCode": nat if nat and nat != "X000" else None,
                    "state": _val(it, "stateCode"),
                    "stage": f"{prev}→{cur}" if prev and cur else None,
                    "trialNumber": _val(it, "trialNumber"),
                    "oppositionNumber": _val(it, "demurrerNumber"),
                }
            )
        )
    events.sort(key=lambda e: (e.get("date") or "", e.get("seq") or 0))
    last = rows[-1]
    return _drop_none(
        {
            "applicationNumber": format_kr_number(_val(first, "applicationNumber")),
            "applicationDate": normalize_date(_val(first, "applicationDate")),
            "openNumber": format_kr_number(_val(last, "openNumber")),
            "openDate": normalize_date(_val(last, "openingDate")),
            "registrationNumber": format_kr_register_number(_val(last, "registrationNumber")),
            "registrationDate": normalize_date(_val(last, "registrationDate")),
            "events": events,
        }
    )


# ---------------------------------------------------------------------------
# 특허 패밀리 (patFamInfoSearchService/getAppNoPatFamInfoSearch, kipo-api)
# ---------------------------------------------------------------------------


def parse_family(xml_text: str) -> dict | None:
    root = _root(xml_text)
    if root is None:
        return None
    members = []
    family_ids: list[str] = []
    for it in root.iter("item"):
        pub_no = _val(it, "publicationNumber")
        app_no = _val(it, "applicationNumber")
        if not pub_no and not app_no:
            continue
        fid = _val(it, "docdbFamilyID")
        if fid and fid not in family_ids:
            family_ids.append(fid)
        pc, pk = _val(it, "publicationCountryCode"), _val(it, "publicationKindCode")
        ac, ak = _val(it, "applicationCountryCode"), _val(it, "applicationKindCode")
        members.append(
            _drop_none(
                {
                    "country": pc or ac,
                    "publication": " ".join(x for x in (pc, pub_no, pk) if x) if pub_no else None,
                    "publicationDate": normalize_date(_val(it, "publicationDate")),
                    "application": " ".join(x for x in (ac, app_no, ak) if x) if app_no else None,
                    "applicationDate": normalize_date(_val(it, "applicationDate")),
                }
            )
        )
    if not members:
        return None
    countries: list[str] = []
    for m in members:
        c = m.get("country")
        if c and c not in countries:
            countries.append(c)
    return _drop_none({"docdbFamilyIds": family_ids, "countries": countries, "members": members})

